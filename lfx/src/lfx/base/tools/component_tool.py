from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Literal

import pandas as pd
from langchain_core.tools import BaseTool, ToolException
from langchain_core.tools.structured import StructuredTool

from lfx.base.tools.constants import TOOL_OUTPUT_NAME
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.serialization.serialization import serialize

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.callbacks import Callbacks

    from lfx.custom.custom_component.component import Component
    from lfx.events.event_manager import EventManager
    from lfx.inputs.inputs import InputTypes
    from lfx.io import Output
    from lfx.schema.content_block import ContentBlock
    from lfx.schema.dotdict import dotdict

TOOL_TYPES_SET = {"Tool", "BaseTool", "StructuredTool"}


def _get_input_type(input_: InputTypes):
    if input_.input_types:
        if len(input_.input_types) == 1:
            return input_.input_types[0]
        return " | ".join(input_.input_types)
    return input_.field_type


def build_description(component: Component) -> str:
    return component.description or ""


async def send_message_noop(
    message: Message,
    text: str | None = None,  # noqa: ARG001
    background_color: str | None = None,  # noqa: ARG001
    text_color: str | None = None,  # noqa: ARG001
    icon: str | None = None,  # noqa: ARG001
    content_blocks: list[ContentBlock] | None = None,  # noqa: ARG001
    format_type: Literal["default", "error", "warning", "info"] = "default",  # noqa: ARG001
    id_: str | None = None,  # noqa: ARG001
    *,
    allow_markdown: bool = True,  # noqa: ARG001
) -> Message:
    """No-op implementation of send_message."""
    return message


def patch_components_send_message(component: Component):
    old_send_message = component.send_message
    component.send_message = send_message_noop  # type: ignore[method-assign, assignment]
    return old_send_message


def _patch_send_message_decorator(component, func):
    """Decorator to patch the send_message method of a component.

    This is useful when we want to use a component as a tool, but we don't want to
    send any messages to the UI. With this only the Component calling the tool
    will send messages to the UI.
    """

    async def async_wrapper(*args, **kwargs):
        original_send_message = component.send_message
        component.send_message = send_message_noop
        try:
            return await func(*args, **kwargs)
        finally:
            component.send_message = original_send_message

    def sync_wrapper(*args, **kwargs):
        original_send_message = component.send_message
        component.send_message = send_message_noop
        try:
            return func(*args, **kwargs)
        finally:
            component.send_message = original_send_message

    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


def _build_output_function(component: Component, output_method: Callable, event_manager: EventManager | None = None):
    def output_function(*args, **kwargs):
        try:
            if event_manager:
                event_manager.on_build_start(data={"id": component.get_id()})
            component.set(*args, **kwargs)
            result = output_method()
            if event_manager:
                event_manager.on_build_end(data={"id": component.get_id()})
        except Exception as e:
            raise ToolException(e) from e

        if isinstance(result, Message):
            return result.get_text()
        if isinstance(result, Data):
            return result.data
        # removing the model_dump() call here because it is not serializable
        return serialize(result)

    return _patch_send_message_decorator(component, output_function)


def _build_output_async_function(
    component: Component, output_method: Callable, event_manager: EventManager | None = None
):
    async def output_function(*args, **kwargs):
        try:
            if event_manager:
                await asyncio.to_thread(event_manager.on_build_start, data={"id": component.get_id()})
            component.set(*args, **kwargs)
            result = await output_method()
            if event_manager:
                await asyncio.to_thread(event_manager.on_build_end, data={"id": component.get_id()})
        except Exception as e:
            raise ToolException(e) from e
        if isinstance(result, Message):
            return result.get_text()
        if isinstance(result, Data):
            return result.data
        # removing the model_dump() call here because it is not serializable
        return serialize(result)

    return _patch_send_message_decorator(component, output_function)


def _format_tool_name(name: str):
    # format to '^[a-zA-Z0-9_-]+$'."
    # to do that we must remove all non-alphanumeric characters

    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


def _add_commands_to_tool_description(tool_description: str, commands: str):
    return f"very_time you see one of those commands {commands} run the tool. tool description is {tool_description}"


class ComponentToolkit:
    def __init__(self, component: Component, metadata: pd.DataFrame | None = None):
        self.component = component
        self.metadata = metadata

    def _should_skip_output(self, output: Output) -> bool:
        """Determines if an output should be skipped when creating tools.

        Args:
            output (Output): The output to check.

        Returns:
            bool: True if the output should be skipped, False otherwise.

        The output will be skipped if:
        - tool_mode is False (output is not meant to be used as a tool)
        - output name matches TOOL_OUTPUT_NAME
        - output types contain any of the tool types in TOOL_TYPES_SET
        """
        return not output.tool_mode or (
            output.name == TOOL_OUTPUT_NAME or any(tool_type in output.types for tool_type in TOOL_TYPES_SET)
        )

    def _attach_runtime_metadata(
        self,
        tool: BaseTool | StructuredTool,
        output: Output,
        *,
        is_async: bool,
    ) -> None:
        """Annotate each tool with component metadata used for remote execution."""
        from lfx.log.logger import logger
        
        print(f"[TOOLKIT] _attach_runtime_metadata called for tool '{tool.name}' (component={self.component.__class__.__name__})", flush=True)
        metadata = tool.metadata or {}
        metadata["_component_method"] = output.method
        metadata["_component_is_async"] = is_async
        metadata["_component_ref"] = self.component
        metadata["component_class"] = self.component.__class__.__name__
        metadata["component_module"] = self.component.__class__.__module__
        metadata["component_id"] = self.component.get_id()
        
        # Get stream_topic - try _build_stream_topic() first, then fallback to stored value from backend
        # Check if method exists first to avoid triggering __getattr__
        if hasattr(self.component, "_build_stream_topic"):
            try:
                stream_topic = self.component._build_stream_topic()  # noqa: SLF001
            except Exception:
                # If _build_stream_topic exists but fails, fallback
                stream_topic = getattr(self.component, "_stream_topic_from_backend", None)
        else:
            # Component doesn't have _build_stream_topic (e.g., AgentQL on tool executor node)
            # Use stream_topic stored from backend request
            stream_topic = getattr(self.component, "_stream_topic_from_backend", None)
        
        if not stream_topic:
            # Last resort: generate a default stream topic
            component_id = self.component.get_id()
            stream_topic = f"droq.local.public.default.{component_id}.out"
            print(f"[TOOLKIT] ⚠️ No stream_topic available, using default: {stream_topic}", flush=True)
        
        metadata["stream_topic"] = stream_topic
        print(f"[TOOLKIT] Using stream_topic: {stream_topic}", flush=True)
        
        # Serialize component state including parameters for remote execution
        print(f"[TOOLKIT] Serializing component state for tool '{tool.name}'...", flush=True)
        
        # DEBUG: Check if component has api_key before serializing
        if self.component.__class__.__name__ == "AgentQL":
            if hasattr(self.component, "api_key"):
                api_key_attr = getattr(self.component, "api_key", None)
                print(f"[TOOLKIT] 🎯 AgentQL component.api_key attribute BEFORE _serialize_for_executor: {repr(api_key_attr)}", flush=True)
            else:
                print(f"[TOOLKIT] ⚠️ AgentQL component has NO api_key attribute!", flush=True)
        
        try:
            component_state = self.component._serialize_for_executor()  # noqa: SLF001
            # CRITICAL: Ensure stream_topic is in component_state (it's needed for remote execution)
            if "stream_topic" not in component_state:
                component_state["stream_topic"] = stream_topic
            
            params = component_state.get("parameters", {})
            print(f"[TOOLKIT] Serialized component state for tool '{tool.name}': {len(params)} parameters ({list(params.keys()) if params else 'NONE'})", flush=True)
            logger.info(
                "Serialized component state for tool '%s': %d parameters (%s)",
                tool.name,
                len(params),
                list(params.keys()) if params else "NONE",
            )
            if not params:
                print(f"[TOOLKIT] WARNING: Tool '{tool.name}' has NO parameters in serialized component_state!", flush=True)
                logger.warning(
                    "Tool '%s' has NO parameters in serialized component_state - this may cause remote execution to fail!",
                    tool.name,
                )
            else:
                # Log parameter values - SHOW ACTUAL AgentQL API KEY
                param_preview = {}
                for key, value in params.items():
                    if "key" in key.lower() or "secret" in key.lower() or "password" in key.lower():
                        # PRINT ACTUAL VALUE FOR AgentQL API KEY
                        if self.component.__class__.__name__ == "AgentQL" and key == "api_key":
                            print(f"[TOOLKIT] 🎯 AgentQL API KEY in component_state['parameters']: {repr(value)}", flush=True)
                            logger.info(f"[TOOLKIT] 🎯 AgentQL API KEY: {repr(value)}")
                            param_preview[key] = f"VALUE={repr(value)}" if value else "MISSING/None"
                        else:
                            param_preview[key] = "***" if value else "MISSING/None"
                    else:
                        param_preview[key] = str(value)[:50] if value is not None else "None"
                print(f"[TOOLKIT] Tool '{tool.name}' parameter values: {param_preview}", flush=True)
                logger.info(
                    "Tool '%s' parameter values: %s",
                    tool.name,
                    param_preview,
                )
            # Log that stream_topic is in component_state
            print(f"[TOOLKIT] ✅ stream_topic in component_state: {component_state.get('stream_topic')}", flush=True)
            metadata["_component_state"] = component_state
            print(f"[TOOLKIT] ✅ Attached _component_state to tool '{tool.name}' metadata (keys: {list(metadata.keys())})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[TOOLKIT] ❌ Failed to serialize component state for tool '{tool.name}': {exc}", flush=True)
            import traceback
            print(f"[TOOLKIT] Traceback: {traceback.format_exc()}", flush=True)
            logger.warning(
                "Failed to serialize component state for tool '%s': %s",
                tool.name,
                exc,
            )
        
        # Note: We don't attach executor_node metadata here because:
        # 1. Components on tool executor node don't have get_executor_node_metadata()
        # 2. The registry service will determine executor routing when the agent uses the tool
        # 3. The agent will query the registry based on component_class to find the executor node
        
        tool.metadata = metadata
        print(f"[TOOLKIT] ✅ Final metadata for tool '{tool.name}': {list(tool.metadata.keys())}", flush=True)

    def get_tools(
        self,
        tool_name: str | None = None,
        tool_description: str | None = None,
        callbacks: Callbacks | None = None,
        flow_mode_inputs: list[dotdict] | None = None,
    ) -> list[BaseTool]:
        from lfx.io.schema import create_input_schema, create_input_schema_from_dict

        tools = []
        for output in self.component.outputs:
            if self._should_skip_output(output):
                continue

            if not output.method:
                msg = f"Output {output.name} does not have a method defined"
                raise ValueError(msg)

            output_method: Callable = getattr(self.component, output.method)
            args_schema = None
            tool_mode_inputs = [_input for _input in self.component.inputs if getattr(_input, "tool_mode", False)]
            if flow_mode_inputs:
                args_schema = create_input_schema_from_dict(
                    inputs=flow_mode_inputs,
                    param_key="flow_tweak_data",
                )
            elif tool_mode_inputs:
                args_schema = create_input_schema(tool_mode_inputs)
            elif output.required_inputs:
                inputs = [
                    self.component.get_underscore_inputs()[input_name]
                    for input_name in output.required_inputs
                    if getattr(self.component, input_name) is None
                ]
                # If any of the required inputs are not in tool mode, this means
                # that when the tool is called it will raise an error.
                # so we should raise an error here.
                # TODO: This logic might need to be improved, example if the required is an api key.
                if not all(getattr(_input, "tool_mode", False) for _input in inputs):
                    non_tool_mode_inputs = [
                        input_.name
                        for input_ in inputs
                        if not getattr(input_, "tool_mode", False) and input_.name is not None
                    ]
                    non_tool_mode_inputs_str = ", ".join(non_tool_mode_inputs)
                    msg = (
                        f"Output '{output.name}' requires inputs that are not in tool mode. "
                        f"The following inputs are not in tool mode: {non_tool_mode_inputs_str}. "
                        "Please ensure all required inputs are set to tool mode."
                    )
                    raise ValueError(msg)
                args_schema = create_input_schema(inputs)

            else:
                args_schema = create_input_schema(self.component.inputs)

            name = f"{output.method}".strip(".")
            formatted_name = _format_tool_name(name)
            event_manager = self.component.get_event_manager()
            if asyncio.iscoroutinefunction(output_method):
                tools.append(
                    StructuredTool(
                        name=formatted_name,
                        description=build_description(self.component),
                        coroutine=_build_output_async_function(self.component, output_method, event_manager),
                        args_schema=args_schema,
                        handle_tool_error=True,
                        callbacks=callbacks,
                        tags=[formatted_name],
                        metadata={
                            "display_name": formatted_name,
                            "display_description": build_description(self.component),
                        },
                    )
                )
                self._attach_runtime_metadata(tools[-1], output, is_async=True)
            else:
                tools.append(
                    StructuredTool(
                        name=formatted_name,
                        description=build_description(self.component),
                        func=_build_output_function(self.component, output_method, event_manager),
                        args_schema=args_schema,
                        handle_tool_error=True,
                        callbacks=callbacks,
                        tags=[formatted_name],
                        metadata={
                            "display_name": formatted_name,
                            "display_description": build_description(self.component),
                        },
                    )
                )
                self._attach_runtime_metadata(tools[-1], output, is_async=False)
        if len(tools) == 1 and (tool_name or tool_description):
            tool = tools[0]
            tool.name = _format_tool_name(str(tool_name)) or tool.name
            tool.description = tool_description or tool.description
            tool.tags = [tool.name]
        elif flow_mode_inputs and (tool_name or tool_description):
            for tool in tools:
                tool.name = _format_tool_name(str(tool_name) + "_" + str(tool.name)) or tool.name
                tool.description = (
                    str(tool_description) + " Output details: " + str(tool.description)
                ) or tool.description
                tool.tags = [tool.name]
        elif tool_name or tool_description:
            msg = (
                "When passing a tool name or description, there must be only one tool, "
                f"but {len(tools)} tools were found."
            )
            raise ValueError(msg)
        return tools

    def get_tools_metadata_dictionary(self) -> dict:
        if isinstance(self.metadata, pd.DataFrame):
            try:
                return {
                    record["tags"][0]: record
                    for record in self.metadata.to_dict(orient="records")
                    if record.get("tags")
                }
            except (KeyError, IndexError) as e:
                msg = "Error processing metadata records: " + str(e)
                raise ValueError(msg) from e
        return {}

    def update_tools_metadata(
        self,
        tools: list[BaseTool | StructuredTool],
    ) -> list[BaseTool]:
        # update the tool_name and description according to the name and secriotion mentioned in the list
        if isinstance(self.metadata, pd.DataFrame):
            metadata_dict = self.get_tools_metadata_dictionary()
            filtered_tools = []
            for tool in tools:
                if isinstance(tool, StructuredTool | BaseTool) and tool.tags:
                    try:
                        tag = tool.tags[0]
                    except IndexError:
                        msg = "Tool tags cannot be empty."
                        raise ValueError(msg) from None
                    if tag in metadata_dict:
                        tool_metadata = metadata_dict[tag]
                        # Only include tools with status=True
                        if tool_metadata.get("status", True):
                            tool.name = tool_metadata.get("name", tool.name)
                            tool.description = tool_metadata.get("description", tool.description)
                            if tool_metadata.get("commands"):
                                tool.description = _add_commands_to_tool_description(
                                    tool.description, tool_metadata.get("commands")
                                )
                            filtered_tools.append(tool)
                else:
                    msg = f"Expected a StructuredTool or BaseTool, got {type(tool)}"
                    raise TypeError(msg)
            return filtered_tools
        return tools
