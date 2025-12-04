"""FastAPI application for Langflow component executor."""

import asyncio
import importlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC
from typing import Any

from fastapi import FastAPI, HTTPException
from langchain_core.tools import BaseTool, Tool
from pydantic import BaseModel

# Add lfx to Python path if it exists in the node directory
_node_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_lfx_path = os.path.join(_node_dir, "lfx", "src")
if os.path.exists(_lfx_path) and _lfx_path not in sys.path:
    sys.path.insert(0, _lfx_path)

logger = logging.getLogger(__name__)
if os.path.exists(_lfx_path):
    logger.debug("Added lfx to Python path: %s", _lfx_path)

_SENSITIVE_PARAM_HINTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "auth",
    "bearer",
    "credential",
)


def _mask_sensitive_value(key: str, value: Any) -> Any:
    """Mask potentially sensitive values for logging purposes."""
    key_lower = key.lower() if isinstance(key, str) else ""
    if isinstance(value, str):
        if any(hint in key_lower for hint in _SENSITIVE_PARAM_HINTS):
            if not value:
                return ""
            if len(value) <= 8:
                return "*" * len(value)
            return f"{value[:4]}...{value[-4:]} (len={len(value)})"
        return value
    if isinstance(value, dict | list):
        return f"<{type(value).__name__}:{len(value)}>"
    return value


def _summarize_parameters(stage: str, params: dict[str, Any]) -> None:
    """Log a sanitized snapshot of component parameters."""
    summary = {key: _mask_sensitive_value(key, value) for key, value in params.items()}
    logger.debug("[%s] parameter snapshot: %s", stage, summary)


def _has_meaningful_value(value: Any) -> bool:
    """Return True if the value should be considered a real override."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, list | tuple | set | dict):
        return len(value) > 0
    return True


def _merge_runtime_inputs(
    base_params: dict[str, Any], runtime_inputs: dict[str, Any] | None
) -> tuple[int, int]:
    """Merge deserialized runtime inputs without clobbering populated parameters."""
    if not runtime_inputs:
        return (0, 0)

    applied = 0
    skipped_empty = 0

    for key, value in runtime_inputs.items():
        incoming_has_value = _has_meaningful_value(value)
        existing_has_value = _has_meaningful_value(base_params.get(key))

        if not incoming_has_value and existing_has_value:
            skipped_empty += 1
            continue

        if not incoming_has_value and not existing_has_value:
            # Nothing to override and nothing useful to add.
            continue

        base_params[key] = value
        applied += 1

    return (applied, skipped_empty)


# Load component mapping from JSON file
_components_json_path = os.path.join(_node_dir, "node.json")
_component_map: dict[str, str] = {}
print(f"[EXECUTOR] Looking for node.json at: {_components_json_path}")
print(f"[EXECUTOR] Node dir: {_node_dir}")
if os.path.exists(_components_json_path):
    try:
        with open(_components_json_path) as f:
            node_data = json.load(f)
        # Extract components mapping from node.json structure
        # node.json has structure: {"components": {"ComponentName": {"path": "...", ...}, ...}}
        # Paths in node.json incorrectly have format "lfx.src.lfx.components..."
        # but should be "lfx.components..." (matching old components.json format)
        if "components" in node_data and isinstance(node_data["components"], dict):
            _component_map = {}
            for component_name, component_info in node_data["components"].items():
                if isinstance(component_info, dict) and "path" in component_info:
                    path = component_info.get("path", "")
                    # Transform path: "lfx.src.lfx.components..." -> "lfx.components..."
                    # Remove the incorrect "lfx.src.lfx." prefix or "lfx.src." prefix
                    original_path = path
                    if path.startswith("lfx.src.lfx."):
                        path = "lfx." + path[len("lfx.src.lfx.") :]
                    elif path.startswith("lfx.src."):
                        path = "lfx." + path[len("lfx.src.") :]
                    if original_path != path:
                        logger.debug(
                            f"Transformed path for {component_name}: " f"{original_path} -> {path}"
                        )
                    _component_map[component_name] = path
            print(
                f"[EXECUTOR] ✅ Loaded {len(_component_map)} component mappings "
                f"from {_components_json_path}"
            )
            logger.info(
                f"Loaded {len(_component_map)} component mappings from {_components_json_path}"
            )
        else:
            logger.warning(
                f"node.json does not contain 'components' key or invalid structure "
                f"at {_components_json_path}"
            )
    except Exception as e:
        print(f"[EXECUTOR] ❌ Failed to load node.json: {e}")
        logger.warning(f"Failed to load node.json: {e}")
else:
    print(f"[EXECUTOR] ❌ node.json not found at {_components_json_path}")
    logger.warning(f"node.json not found at {_components_json_path}")

app = FastAPI(title="Langflow Executor Node", version="0.1.0")

# Initialize NATS client (lazy connection)
_nats_client = None


async def get_nats_client():
    """Get or create NATS client instance."""
    global _nats_client
    if _nats_client is None:
        logger.info("[NATS] Creating new NATS client instance...")
        from node.nats import NATSClient

        nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
        logger.info(f"[NATS] Connecting to NATS at {nats_url}")
        _nats_client = NATSClient(nats_url=nats_url)
        try:
            await _nats_client.connect()
            logger.info("[NATS] ✅ Successfully connected to NATS")
        except Exception as e:
            logger.warning(
                f"[NATS] ❌ Failed to connect to NATS (non-critical): {e}",
                exc_info=True,
            )
            _nats_client = None
    else:
        logger.debug("[NATS] Using existing NATS client instance")
    return _nats_client


class ComponentState(BaseModel):
    """Component state for execution."""

    component_class: str
    component_module: str
    component_code: str | None = None
    parameters: dict[str, Any]
    input_values: dict[str, Any] | None = None  # Current input values from upstream components
    config: dict[str, Any] | None = None
    display_name: str | None = None
    component_id: str | None = None
    stream_topic: str | None = None  # NATS stream topic for publishing results


class ExecutionRequest(BaseModel):
    """Request to execute a component method."""

    component_state: ComponentState
    method_name: str
    is_async: bool = False
    timeout: int = 30
    message_id: str | None = None  # Unique message ID from backend for tracking published messages


class ExecutionResponse(BaseModel):
    """Response from component execution."""

    result: Any
    success: bool
    result_type: str
    execution_time: float
    error: str | None = None
    message_id: str | None = None  # Unique ID for the published NATS message


async def load_component_class(
    module_name: str, class_name: str, component_code: str | None = None
) -> type:
    """
    Dynamically load a component class.

    Args:
        module_name: Python module path (e.g., "lfx.components.input_output.text")
        class_name: Class name (e.g., "TextInputComponent")
        component_code: Optional source code to execute if module loading fails

    Returns:
        Component class

    Raises:
        HTTPException: If module or class cannot be loaded
    """
    # If module path is wrong (validation wrapper), try to find the correct module
    # from node.json
    if module_name in ("lfx.custom.validate", "lfx.custom.custom_component.component"):
        print(
            f"[EXECUTOR] Module path is incorrect ({module_name}), "
            f"looking up {class_name} in node.json (map size: {len(_component_map)})"
        )
        logger.info(
            f"Module path is incorrect ({module_name}), "
            f"looking up correct module for {class_name} in node.json"
        )

        # Look up the correct module path from the JSON mapping
        if class_name in _component_map:
            correct_module = _component_map[class_name]
            print(f"[EXECUTOR] ✅ Found mapping: {class_name} -> {correct_module}")
            logger.info(f"Found module mapping: {class_name} -> {correct_module}")
            try:
                module = importlib.import_module(correct_module)
                component_class = getattr(module, class_name)
                print(f"[EXECUTOR] ✅ Successfully loaded {class_name} from {correct_module}")
                logger.info(f"Successfully loaded {class_name} from mapped module {correct_module}")
                return component_class
            except (ImportError, AttributeError) as e:
                print(f"[EXECUTOR] ❌ Failed to load {class_name} from {correct_module}: {e}")
                logger.warning(
                    f"Failed to load {class_name} from mapped module " f"{correct_module}: {e}"
                )
                # Fall back to code execution if module import fails
                if component_code:
                    print(f"[EXECUTOR] Falling back to code execution for {class_name}")
                    logger.info(f"Falling back to code execution for {class_name}")
                    try:
                        return await load_component_from_code(component_code, class_name)
                    except Exception as code_error:
                        logger.error(f"Code execution also failed for {class_name}: {code_error}")
                        # Continue to next fallback attempt
        else:
            print(
                f"[EXECUTOR] ❌ Component {class_name} not found in node.json "
                f"(available: {list(_component_map.keys())[:5]}...)"
            )
            logger.warning(f"Component {class_name} not found in node.json mapping")

    # First try loading from the provided module path
    try:
        module = importlib.import_module(module_name)
        component_class = getattr(module, class_name)
        logger.info(f"Successfully loaded {class_name} from module {module_name}")
        return component_class
    except ImportError as e:
        logger.warning(f"Failed to import module {module_name}: {e}")
        # If module import fails and we have code, try executing code
        if component_code:
            logger.info(f"Attempting to load {class_name} from provided code")
            return await load_component_from_code(component_code, class_name)
        raise HTTPException(status_code=400, detail=f"Failed to import module {module_name}: {e}")
    except AttributeError as e:
        logger.warning(f"Class {class_name} not found in module {module_name}: {e}")
        # If class not found and we have code, try executing code
        if component_code:
            logger.info(
                f"Attempting to load {class_name} from provided code "
                f"(code length: {len(component_code)} chars)"
            )
        else:
            logger.error(
                f"No component_code provided! Cannot fallback to code execution. "
                f"Module={module_name}, Class={class_name}"
            )
        # Try to use code if available
        if component_code:
            try:
                return await load_component_from_code(component_code, class_name)
            except HTTPException as code_error:
                # Provide more context in the error
                logger.error(
                    f"Failed to load from code: {code_error.detail}. "
                    f"Module path was: {module_name}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Class {class_name} not found in module {module_name} "
                        f"and code execution failed: {code_error.detail}"
                    ),
                )
        raise HTTPException(
            status_code=400,
            detail=f"Class {class_name} not found in module {module_name}: {e}",
        )


async def load_component_from_code(component_code: str, class_name: str) -> type:
    """
    Load a component class by executing its source code.

    Args:
        component_code: Python source code containing the component class
        class_name: Name of the class to extract

    Returns:
        Component class

    Raises:
        HTTPException: If code execution fails or class not found
    """
    try:
        # Create a new namespace for code execution
        # Import common Langflow modules that components might need
        namespace = {
            "__builtins__": __builtins__,
        }

        # Try to import common Langflow modules into the namespace
        try:
            import lfx.base.io.text
            import lfx.io
            import lfx.schema.message

            namespace["lfx"] = __import__("lfx")
            namespace["lfx.base"] = __import__("lfx.base")
            namespace["lfx.base.io"] = __import__("lfx.base.io")
            namespace["lfx.base.io.text"] = lfx.base.io.text
            namespace["lfx.io"] = lfx.io
            namespace["lfx.schema"] = __import__("lfx.schema")
            namespace["lfx.schema.message"] = lfx.schema.message
        except Exception as import_error:
            logger.warning(f"Could not pre-import some modules: {import_error}")

        exec(compile(component_code, "<string>", "exec"), namespace)

        if class_name not in namespace:
            # Log what classes are available in the namespace
            available_classes = [
                k for k, v in namespace.items() if isinstance(v, type) and not k.startswith("_")
            ]
            logger.error(
                f"Class {class_name} not found in provided code. "
                f"Available classes: {available_classes[:10]}"
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Class {class_name} not found in provided code. "
                    f"Available classes: {', '.join(available_classes[:5])}"
                ),
            )

        component_class = namespace[class_name]
        logger.info(f"Successfully loaded {class_name} from provided code")
        return component_class
    except SyntaxError as e:
        logger.error(f"Syntax error in component code: {e}")
        raise HTTPException(status_code=400, detail=f"Syntax error in component code: {e}")
    except Exception as e:
        logger.error(f"Error executing component code: {e}")
        raise HTTPException(
            status_code=400, detail=f"Error executing component code: {type(e).__name__}: {e}"
        )


def serialize_result(result: Any) -> Any:
    """
    Serialize component execution result, preserving Tool metadata including _component_state.

    Args:
        result: Component execution result

    Returns:
        Serialized result
    """
    # Handle None
    if result is None:
        return None

    # Handle LangChain Tool objects FIRST - explicitly preserve metadata
    if isinstance(result, BaseTool):
        tool_name = getattr(result, "name", "unknown")
        print(f"[SERIALIZE_RESULT] 🔧 Serializing Tool '{tool_name}'", flush=True)
        try:
            tool_dict = {}
            # Get base tool attributes
            if hasattr(result, "model_dump"):
                tool_dict = result.model_dump()
            elif hasattr(result, "dict"):
                tool_dict = result.dict()
            else:
                tool_dict = {
                    "name": getattr(result, "name", ""),
                    "description": getattr(result, "description", ""),
                }

            # CRITICAL: Explicitly include metadata (model_dump might not include it)
            if hasattr(result, "metadata") and result.metadata:
                print(
                    f"[SERIALIZE_RESULT] 🔧 Tool '{tool_name}' has metadata: "
                    f"{list(result.metadata.keys())}",
                    flush=True,
                )
                if "_component_state" in result.metadata:
                    comp_state = result.metadata["_component_state"]
                    if isinstance(comp_state, dict) and "parameters" in comp_state:
                        params = comp_state["parameters"]
                        api_key_val = params.get("api_key") if isinstance(params, dict) else None
                        print(
                            f"[SERIALIZE_RESULT] 🎯 Tool '{tool_name}' "
                            f"_component_state['parameters']['api_key'] = {repr(api_key_val)}",
                            flush=True,
                        )
                tool_dict["metadata"] = serialize_result(result.metadata)
            else:
                print(f"[SERIALIZE_RESULT] ⚠️ Tool '{tool_name}' has NO metadata!", flush=True)
                tool_dict["metadata"] = {}

            # Recursively serialize all values
            serialized = {k: serialize_result(v) for k, v in tool_dict.items()}
            print(
                f"[SERIALIZE_RESULT] ✅ Serialized Tool '{tool_name}': metadata keys = "
                f"{list(serialized.get('metadata', {}).keys())}",
                flush=True,
            )
            if "_component_state" in serialized.get("metadata", {}):
                print(
                    f"[SERIALIZE_RESULT] ✅ Tool '{tool_name}' _component_state "
                    f"preserved in serialized result!",
                    flush=True,
                )
            return serialized
        except Exception as exc:
            print(
                f"[SERIALIZE_RESULT] ❌ Failed to serialize tool '{tool_name}': {exc}",
                flush=True,
            )
            import traceback

            print(f"[SERIALIZE_RESULT] Traceback: {traceback.format_exc()}", flush=True)
            logger.warning(f"Failed to serialize tool '{tool_name}': {exc}")
            # Fallback: return minimal representation with metadata
            return {
                "name": getattr(result, "name", ""),
                "description": getattr(result, "description", ""),
                "metadata": serialize_result(getattr(result, "metadata", {})),
            }

    # Handle primitive types
    if isinstance(result, str | int | float | bool):
        return result

    # Skip type/metaclass objects - they can't be serialized
    if isinstance(result, type):
        # Return the class name as a string representation
        return f"<class '{result.__module__}.{result.__name__}'>"

    # Check for Pydantic metaclass specifically
    result_type_str = str(type(result))
    if "ModelMetaclass" in result_type_str or "metaclass" in result_type_str.lower():
        return f"<metaclass: {getattr(result, '__name__', type(result).__name__)}>"

    # Handle lists/tuples first (before other checks)
    if isinstance(result, list | tuple):
        return [serialize_result(item) for item in result]

    # Handle dicts
    if isinstance(result, dict):
        return {k: serialize_result(v) for k, v in result.items()}

    # Handle common Langflow types (Pydantic models)
    if hasattr(result, "model_dump"):
        try:
            dumped = result.model_dump()
            # Recursively serialize the dumped result to catch any nested issues
            return serialize_result(dumped)
        except Exception as e:
            logger.debug(f"model_dump failed: {e}, trying dict()")
            # If model_dump fails, try dict()
            pass
    if hasattr(result, "dict"):
        try:
            dumped = result.dict()
            return serialize_result(dumped)
        except Exception as e:
            logger.debug(f"dict() failed: {e}")
            pass

    # Try to serialize via __dict__ (but skip private attributes and classes)
    if hasattr(result, "__dict__"):
        try:
            serialized_dict = {}
            for k, v in result.__dict__.items():
                # Skip private attributes except __class__
                if k.startswith("_") and k != "__class__":
                    continue
                # Skip type objects
                if isinstance(v, type):
                    continue
                serialized_dict[k] = serialize_result(v)
            return serialized_dict
        except Exception as e:
            logger.debug(f"__dict__ serialization failed: {e}")
            pass

    # For callable objects (functions, methods), return string representation
    if callable(result):
        return f"<callable: {getattr(result, '__name__', type(result).__name__)}>"

    # Last resort: try to convert to string
    try:
        return str(result)
    except Exception:
        return f"<unserializable: {type(result).__name__}>"


def deserialize_input_value(value: Any) -> Any:
    """
    Deserialize input value, reconstructing Langflow types from dicts.

    Args:
        value: Serialized input value (may be a dict representing Data/Message)

    Returns:
        Deserialized value with proper types reconstructed
    """
    if not isinstance(value, dict):
        # Recursively handle lists
        if isinstance(value, list):
            return [deserialize_input_value(item) for item in value]
        return value

    # Try to reconstruct Data or Message objects
    try:
        from lfx.schema.data import Data
        from lfx.schema.message import Message

        # Check if it looks like a Message (has Message-specific fields)
        # Message extends Data, so it has text_key, data, and Message-specific fields
        # like sender, category, duration, etc.
        message_fields = [
            "sender",
            "category",
            "session_id",
            "timestamp",
            "duration",
            "flow_id",
            "error",
            "edit",
            "sender_name",
            "context_id",
        ]
        has_message_fields = any(key in value for key in message_fields)

        # Also check inside data dict (Message fields might be nested there)
        data_dict = value.get("data", {})
        if isinstance(data_dict, dict):
            has_message_fields_in_data = any(key in data_dict for key in message_fields)
            has_message_fields = has_message_fields or has_message_fields_in_data

        if has_message_fields:
            # Fix timestamp format if present (convert various formats to YYYY-MM-DD HH:MM:SS UTC)
            if "timestamp" in value and isinstance(value["timestamp"], str):
                timestamp = value["timestamp"]
                # Convert ISO format with T separator to space
                # (e.g., "2025-11-14T13:09:23 UTC" -> "2025-11-14 13:09:23 UTC")
                if "T" in timestamp:
                    # Replace T with space, but preserve the UTC part
                    timestamp = timestamp.replace("T", " ")
                # Convert ISO format with timezone to UTC format
                if "+00:00" in timestamp:
                    timestamp = timestamp.replace("+00:00", " UTC")
                elif timestamp.endswith("Z"):
                    timestamp = timestamp.replace("Z", " UTC")
                elif "Z " in timestamp:
                    timestamp = timestamp.replace("Z ", " UTC ")
                # Ensure it ends with UTC if it doesn't already
                if not timestamp.endswith(" UTC") and not timestamp.endswith(" UTC"):
                    # Try to parse and reformat using datetime
                    try:
                        from datetime import datetime

                        # Try common formats
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %Z"]:
                            try:
                                dt = datetime.strptime(timestamp.strip(), fmt)
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=UTC)
                                timestamp = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                                break
                            except ValueError:
                                continue
                    except Exception:
                        pass
                value["timestamp"] = timestamp

            # Create Message object - Message constructor will handle merging fields into data dict
            # according to Data.validate_data logic
            try:
                message_obj = Message(**value)
                logger.debug(
                    f"[DESERIALIZE] Successfully reconstructed Message object from dict "
                    f"with keys: {list(value.keys())}"
                )
                return message_obj
            except Exception as msg_error:
                logger.warning(
                    f"[DESERIALIZE] Failed to create Message from dict: {msg_error}, "
                    f"keys: {list(value.keys())}"
                )
                # Try to create with just the data dict if that exists
                if "data" in value and isinstance(value["data"], dict):
                    try:
                        return Message(
                            data=value["data"],
                            **{k: v for k, v in value.items() if k != "data"},
                        )
                    except Exception:
                        pass
                raise

        # Check if it looks like a Data object (has text_key or data field,
        # but not Message-specific fields)
        if ("data" in value or "text_key" in value) and not has_message_fields:
            return Data(**value)

    except Exception as e:
        logger.debug(f"[DESERIALIZE] Could not reconstruct object from dict: {e}")
        # Return as-is if reconstruction fails
        pass

    # For dicts, recursively deserialize values
    return {k: deserialize_input_value(v) for k, v in value.items()}


def sanitize_tool_inputs(
    component_params: dict[str, Any], component_class: str | None = None
) -> list[BaseTool] | None:
    """Ensure `tools` parameter only contains LangChain tool objects.

    When components (especially agents) run in tool mode, the backend currently
    serializes tool objects into plain dictionaries. Those dictionaries do not
    expose attributes like `.name`, which causes `validate_tool_names` to raise
    AttributeError. Drop any invalid entries so execution can proceed without
    crashing. Workflows that genuinely depend on those tools will still log a
    warning, but at least the agent can run (albeit without tool support).
    """

    tools_value = component_params.get("tools")
    if not tools_value:
        return None

    candidates = tools_value if isinstance(tools_value, list) else [tools_value]
    valid_tools: list[BaseTool] = []
    invalid_types: list[str] = []
    for tool in candidates:
        if isinstance(tool, BaseTool):
            valid_tools.append(tool)
            continue
        reconstructed = reconstruct_tool(tool)
        if reconstructed:
            valid_tools.append(reconstructed)
            continue
        invalid_types.append(type(tool).__name__)

    if invalid_types:
        logger.warning(
            "[%s] Dropping %d invalid tool payload(s); "
            "expected LangChain BaseTool instances, got: %s",
            component_class or "Component",
            len(invalid_types),
            ", ".join(sorted(set(invalid_types))),
        )

    component_params["tools"] = valid_tools
    return valid_tools


def reconstruct_tool(value: Any) -> BaseTool | None:
    """Attempt to rebuild a LangChain tool from serialized metadata."""
    if not isinstance(value, dict):
        return None

    name = value.get("name")
    description = value.get("description", "")
    metadata = value.get("metadata", {})
    if not name:
        return None

    def _tool_func(*args, **kwargs):
        logger.warning(
            "Tool '%s' invoked in executor context; returning placeholder response.",
            name,
        )
        return {
            "tool": name,
            "status": "unavailable",
            "message": (
                "Tool cannot execute inside executor context; " "please route to appropriate node."
            ),
        }

    try:
        # Tool from langchain_core.tools can wrap simple callables
        reconstructed = Tool(
            name=name,
            description=description or metadata.get("display_description", ""),
            func=_tool_func,
            coroutine=None,
        )
        reconstructed.metadata = metadata
        return reconstructed
    except Exception as exc:
        logger.warning("Failed to reconstruct tool '%s': %s", name, exc)
        return None


@app.post("/api/v1/execute", response_model=ExecutionResponse)
async def execute_component(request: ExecutionRequest) -> ExecutionResponse:
    """
    Execute a Langflow component method.

    Args:
        request: Execution request with component state and method name

    Returns:
        Execution response with result or error
    """
    start_time = time.time()

    try:
        # Log what we received
        stream_topic_value = request.component_state.stream_topic
        log_msg = (
            f"Received execution request: "
            f"class={request.component_state.component_class}, "
            f"module={request.component_state.component_module}, "
            f"code_length={len(request.component_state.component_code or '') if request.component_state.component_code else 0}, "  # noqa: E501
            f"stream_topic={stream_topic_value}"
        )
        logger.info(log_msg)
        print(f"[EXECUTOR] {log_msg}")  # Also print to ensure visibility

        # Load component class dynamically
        component_class = await load_component_class(
            request.component_state.component_module,
            request.component_state.component_class,
            request.component_state.component_code,
        )

        # Instantiate component with parameters
        component_params = request.component_state.parameters.copy()

        # DEBUG: Log AgentQL API key if present
        if request.component_state.component_class == "AgentQL" and "api_key" in component_params:
            api_key_val = component_params.get("api_key")
            print(
                f"[EXECUTOR] 🎯 AgentQL API KEY received in component_state.parameters: "
                f"{repr(api_key_val)}",
                flush=True,
            )
            logger.info(f"[EXECUTOR] 🎯 AgentQL API KEY received: {repr(api_key_val)}")

        _summarize_parameters("parameters/base", component_params)

        # Merge input_values (runtime values from upstream components) into parameters
        # These override static parameters since they contain the actual workflow data
        deserialized_inputs: dict[str, Any] = {}
        if request.component_state.input_values:
            # Deserialize input values to reconstruct Data/Message objects
            for key, value in request.component_state.input_values.items():
                deserialized = deserialize_input_value(value)
                # Skip None values to avoid overriding defaults with invalid types
                if deserialized is None:
                    logger.debug(
                        "Skipping None input value for %s to preserve component default", key
                    )
                    continue
                deserialized_inputs[key] = deserialized
            applied, skipped = _merge_runtime_inputs(component_params, deserialized_inputs)
            logger.info(
                "Merged %d deserialized runtime input(s); skipped %d empty value(s)",
                applied,
                skipped,
            )

        if request.component_state.config:
            # Merge config into parameters with _ prefix
            for key, value in request.component_state.config.items():
                component_params[f"_{key}"] = value

        if request.component_state.component_class == "AgentComponent":
            logger.info(
                "[AgentComponent] input keys: %s; tools raw payload: %s",
                list((request.component_state.input_values or {}).keys()),
                (request.component_state.input_values or {}).get("tools"),
            )
            if request.component_state.input_values and request.component_state.input_values.get(
                "tools"
            ):
                sample_tool = request.component_state.input_values["tools"][0]
                logger.debug(
                    "[AgentComponent] Sample tool payload keys: %s",
                    list(sample_tool.keys()),
                )
                logger.debug(
                    "[AgentComponent] Sample tool metadata: %s",
                    sample_tool.get("metadata"),
                )

        logger.info(
            f"Instantiating {request.component_state.component_class} "
            f"with {len(component_params)} parameters "
            f"(static: {len(request.component_state.parameters)}, "
            f"inputs: {len(request.component_state.input_values or {})}, "
            f"config: {len(request.component_state.config or {})})"
        )
        # Drop None values to mimic Langflow's default handling (unset fields)
        if component_params:
            filtered_params = {
                key: value for key, value in component_params.items() if value is not None
            }
            if len(filtered_params) != len(component_params):
                logger.debug(
                    "Removed %d None-valued parameters before instantiation",
                    len(component_params) - len(filtered_params),
                )
            component_params = filtered_params

        # Ensure `tools` parameter contains valid tool instances only
        sanitized_tools = sanitize_tool_inputs(
            component_params, request.component_state.component_class
        )
        if sanitized_tools is not None and "tools" in deserialized_inputs:
            deserialized_inputs["tools"] = sanitized_tools

        _summarize_parameters("parameters/final", component_params)

        # DEBUG: Log api_key before instantiation for AgentQL
        if request.component_state.component_class == "AgentQL" and "api_key" in component_params:
            api_key_val = component_params.get("api_key")
            print(
                f"[EXECUTOR] 🎯 AgentQL api_key in component_params BEFORE instantiation: "
                f"{repr(api_key_val)}",
                flush=True,
            )
            logger.info(f"[EXECUTOR] 🎯 AgentQL api_key in component_params: {repr(api_key_val)}")

        component = component_class(**component_params)

        # DEBUG: Verify api_key is set on component instance
        if request.component_state.component_class == "AgentQL":
            if hasattr(component, "api_key"):
                api_key_attr = getattr(component, "api_key", None)
                print(
                    f"[EXECUTOR] 🎯 AgentQL component.api_key attribute AFTER instantiation: "
                    f"{repr(api_key_attr)}",
                    flush=True,
                )
                logger.info(
                    f"[EXECUTOR] 🎯 AgentQL component.api_key attribute: " f"{repr(api_key_attr)}"
                )
            else:
                print(
                    "[EXECUTOR] ⚠️ AgentQL component has NO api_key attribute "
                    "after instantiation!",
                    flush=True,
                )
                logger.warning("[EXECUTOR] ⚠️ AgentQL component has NO api_key attribute!")

        # Store stream_topic on component so ComponentToolkit can access it
        if request.component_state.stream_topic:
            # Store stream_topic as an attribute so _attach_runtime_metadata can access it
            component._stream_topic_from_backend = (
                request.component_state.stream_topic
            )  # noqa: SLF001

        # Ensure runtime inputs also populate component attributes for template rendering
        if deserialized_inputs:
            try:
                component.set_attributes(deserialized_inputs)
            except Exception as attr_err:
                logger.warning(
                    "Failed to set component attributes from input values (%s): %s",
                    request.component_state.component_class,
                    attr_err,
                )

        # Get the method
        if not hasattr(component, request.method_name):
            raise HTTPException(
                status_code=400,
                detail=f"Method {request.method_name} not found on component",
            )

        method = getattr(component, request.method_name)

        # Execute method (handle async/sync)
        logger.info(
            f"Executing method {request.method_name} "
            f"(async={request.is_async}) on {request.component_state.component_class}"
        )

        # DEBUG: Log if this is to_toolkit for AgentQL
        if (
            request.method_name == "to_toolkit"
            and request.component_state.component_class == "AgentQL"
        ):
            print("[EXECUTOR] 🎯 Executing to_toolkit for AgentQL component", flush=True)
            api_key_in_params = request.component_state.parameters.get("api_key")
            print(
                f"[EXECUTOR] 🎯 AgentQL api_key in component_state.parameters "
                f"BEFORE to_toolkit: {repr(api_key_in_params)}",
                flush=True,
            )
            # Also check if component instance has api_key
            if hasattr(component, "api_key"):
                print(
                    f"[EXECUTOR] 🎯 AgentQL component.api_key attribute: "
                    f"{repr(getattr(component, 'api_key', None))}",
                    flush=True,
                )

        if request.is_async:
            result = await asyncio.wait_for(method(), timeout=request.timeout)
        else:
            # Run sync method in thread pool
            result = await asyncio.wait_for(asyncio.to_thread(method), timeout=request.timeout)

        # DEBUG: Log result after to_toolkit
        if (
            request.method_name == "to_toolkit"
            and request.component_state.component_class == "AgentQL"
        ):
            print(f"[EXECUTOR] 🎯 to_toolkit result type: {type(result)}", flush=True)
            if isinstance(result, list) and len(result) > 0:
                first_tool = result[0]
                print(f"[EXECUTOR] 🎯 First tool type: {type(first_tool)}", flush=True)
                if hasattr(first_tool, "metadata"):
                    print(
                        f"[EXECUTOR] 🎯 First tool metadata keys: "
                        f"{list(first_tool.metadata.keys()) if first_tool.metadata else 'NONE'}",
                        flush=True,
                    )
                    if first_tool.metadata and "_component_state" in first_tool.metadata:
                        comp_state = first_tool.metadata["_component_state"]
                        if isinstance(comp_state, dict) and "parameters" in comp_state:
                            params = comp_state["parameters"]
                            api_key_val = (
                                params.get("api_key") if isinstance(params, dict) else None
                            )
                            print(
                                "[EXECUTOR] 🎯 First tool "
                                "_component_state['parameters']['api_key']: "
                                f"{repr(api_key_val)}",
                                flush=True,
                            )
                    else:
                        print(
                            "[EXECUTOR] ⚠️ First tool has NO _component_state in metadata!",
                            flush=True,
                        )

        execution_time = time.time() - start_time

        # Serialize result
        serialized_result = serialize_result(result)

        logger.info(
            f"Method {request.method_name} completed successfully "
            f"in {execution_time:.3f}s, result type: {type(result).__name__}"
        )

        # Use message_id from request (generated by backend) or generate one if not provided
        message_id = request.message_id or str(uuid.uuid4())

        # Log a concise preview of the serialized result before publishing
        result_preview = str(serialized_result)
        max_length = int(os.getenv("RESULT_PREVIEW_MAX_CHARS", "500"))
        if len(result_preview) > max_length:
            result_preview = f"{result_preview[:max_length]}…"
        logger.info(
            "[RESULT] Prepared output for %s (message_id=%s, type=%s): %s",
            request.component_state.component_class,
            message_id,
            type(result).__name__,
            result_preview,
        )

        # Publish result to NATS stream if topic is provided
        if request.component_state.stream_topic:
            topic = request.component_state.stream_topic
            logger.info(
                f"[NATS] Attempting to publish to topic: {topic} " f"with message_id: {message_id}"
            )
            print(
                f"[NATS] Attempting to publish to topic: {topic} " f"with message_id: {message_id}"
            )
            try:
                nats_client = await get_nats_client()
                if nats_client:
                    logger.info("[NATS] NATS client obtained, preparing publish data...")
                    print("[NATS] NATS client obtained, preparing publish data...")
                    # Publish result to NATS with message ID from backend
                    publish_data = {
                        "message_id": message_id,  # Use message_id from backend request
                        "component_id": request.component_state.component_id,
                        "component_class": request.component_state.component_class,
                        "result": serialized_result,
                        "result_type": type(result).__name__,
                        "execution_time": execution_time,
                    }
                    logger.info(
                        f"[NATS] Publishing to topic: {topic}, message_id: {message_id}, "
                        f"data keys: {list(publish_data.keys())}"
                    )
                    print(
                        f"[NATS] Publishing to topic: {topic}, message_id: {message_id}, "
                        f"data keys: {list(publish_data.keys())}"
                    )
                    # Use the topic directly (already in format:
                    # droq.local.public.userid.workflowid.component.out)
                    await nats_client.publish(topic, publish_data)
                    logger.info(
                        f"[NATS] ✅ Successfully published result to NATS topic: {topic} "
                        f"with message_id: {message_id}"
                    )
                    print(
                        f"[NATS] ✅ Successfully published result to NATS topic: {topic} "
                        f"with message_id: {message_id}"
                    )
                else:
                    logger.warning("[NATS] NATS client is None, cannot publish")
                    print("[NATS] ⚠️  NATS client is None, cannot publish")
            except Exception as e:
                # Non-critical: log but don't fail execution
                logger.warning(
                    f"[NATS] ❌ Failed to publish to NATS (non-critical): {e}",
                    exc_info=True,
                )
                print(f"[NATS] ❌ Failed to publish to NATS (non-critical): {e}")
        else:
            msg = (
                f"[NATS] ⚠️  No stream_topic provided in request, skipping NATS publish. "
                f"Component: {request.component_state.component_class}, "
                f"ID: {request.component_state.component_id}"
            )
            logger.info(msg)
            print(msg)

        return ExecutionResponse(
            result=serialized_result,
            success=True,
            result_type=type(result).__name__,
            execution_time=execution_time,
            message_id=message_id,  # Return message ID (from request or generated)
            # so backend can match it
        )

    except TimeoutError:
        execution_time = time.time() - start_time
        error_msg = f"Execution timed out after {request.timeout}s"
        logger.error(error_msg)
        return ExecutionResponse(
            result=None,
            success=False,
            result_type="TimeoutError",
            execution_time=execution_time,
            error=error_msg,
        )

    except HTTPException:
        raise

    except Exception as e:
        execution_time = time.time() - start_time
        error_msg = f"Execution failed: {type(e).__name__}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return ExecutionResponse(
            result=None,
            success=False,
            result_type=type(e).__name__,
            execution_time=execution_time,
            error=error_msg,
        )


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "langflow-executor-node"}


@app.get("/")
async def root() -> dict[str, Any]:
    """Root endpoint."""
    return {
        "service": "Langflow Tool Executor Node",
        "version": "0.1.0",
        "endpoints": {
            "execute": "/api/v1/execute",
            "health": "/health",
        },
    }
