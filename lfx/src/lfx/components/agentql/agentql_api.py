import httpx

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import BoolInput, DropdownInput, IntInput, MessageTextInput, MultilineInput, Output, SecretStrInput
from lfx.log.logger import logger
from lfx.schema.data import Data


class AgentQL(Component):
    display_name = "Extract Web Data"
    description = "Extracts structured data from a web page using an AgentQL query or a Natural Language description."
    documentation: str = "https://docs.agentql.com/rest-api/api-reference"
    icon = "AgentQL"
    name = "AgentQL"

    inputs = [
        SecretStrInput(
            name="api_key",
            display_name="AgentQL API Key",
            required=True,
            password=True,
            info="Your AgentQL API key from dev.agentql.com",
        ),
        MessageTextInput(
            name="url",
            display_name="URL",
            required=True,
            info="The URL of the public web page you want to extract data from.",
            tool_mode=True,
        ),
        MultilineInput(
            name="query",
            display_name="AgentQL Query",
            required=False,
            info="The AgentQL query to execute. Learn more at https://docs.agentql.com/agentql-query or use a prompt.",
            tool_mode=True,
        ),
        MultilineInput(
            name="prompt",
            display_name="Prompt",
            required=False,
            info="A Natural Language description of the data to extract from the page. Alternative to AgentQL query.",
            tool_mode=True,
        ),
        BoolInput(
            name="is_stealth_mode_enabled",
            display_name="Enable Stealth Mode (Beta)",
            info="Enable experimental anti-bot evasion strategies. May not work for all websites at all times.",
            value=False,
            advanced=True,
        ),
        IntInput(
            name="timeout",
            display_name="Timeout",
            info="Seconds to wait for a request.",
            value=900,
            advanced=True,
        ),
        DropdownInput(
            name="mode",
            display_name="Request Mode",
            info="'standard' uses deep data analysis, while 'fast' trades some depth of analysis for speed.",
            options=["fast", "standard"],
            value="fast",
            advanced=True,
        ),
        IntInput(
            name="wait_for",
            display_name="Wait For",
            info="Seconds to wait for the page to load before extracting data.",
            value=0,
            range_spec=RangeSpec(min=0, max=10, step_type="int"),
            advanced=True,
        ),
        BoolInput(
            name="is_scroll_to_bottom_enabled",
            display_name="Enable scroll to bottom",
            info="Scroll to bottom of the page before extracting data.",
            value=False,
            advanced=True,
        ),
        BoolInput(
            name="is_screenshot_enabled",
            display_name="Enable screenshot",
            info="Take a screenshot before extracting data. Returned in 'metadata' as a Base64 string.",
            value=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Data", name="data", method="build_output"),
    ]

    def build_output(self) -> Data:
        endpoint = "https://api.agentql.com/v1/query-data"
        
        # DEBUG: Log api_key before making request
        api_key_val = getattr(self, "api_key", None)
        print(f"[AgentQL] 🎯 build_output: self.api_key = {repr(api_key_val)}, type = {type(api_key_val)}", flush=True)
        logger.info(f"[AgentQL] 🎯 build_output: self.api_key = {repr(api_key_val)}, type = {type(api_key_val)}")
        
        if not api_key_val:
            raise ValueError("API key is missing or empty. Please provide a valid AgentQL API key.")
        
        # Handle SecretStr objects (from Pydantic) - extract the actual string value
        if hasattr(api_key_val, "get_secret_value"):
            api_key_str = api_key_val.get_secret_value()
            print(f"[AgentQL] 🎯 Extracted SecretStr value: {repr(api_key_str)}", flush=True)
        else:
            api_key_str = str(api_key_val)
        
        # Strip whitespace (common issue with API keys)
        api_key_str = api_key_str.strip()
        
        if not api_key_str:
            raise ValueError("API key is empty after processing. Please provide a valid AgentQL API key.")
        
        # Validate API key format (AgentQL API keys typically don't start with special characters)
        # But we'll allow it since the user's key starts with '-'
        if len(api_key_str) < 10:
            raise ValueError(f"API key appears to be too short (length={len(api_key_str)}). Please provide a valid AgentQL API key.")
        
        # Log the final header value (masked for security)
        print(f"[AgentQL] 🎯 Using API key in header (length={len(api_key_str)}, starts_with={api_key_str[:5] if len(api_key_str) >= 5 else 'N/A'})", flush=True)
        print(f"[AgentQL] 🎯 API key ends_with={api_key_str[-5:] if len(api_key_str) >= 5 else 'N/A'}", flush=True)
        
        headers = {
            "X-API-Key": api_key_str,
            "Content-Type": "application/json",
            "X-TF-Request-Origin": "langflow",
        }

        payload = {
            "url": self.url,
            "query": self.query,
            "prompt": self.prompt,
            "params": {
                "mode": self.mode,
                "wait_for": self.wait_for,
                "is_scroll_to_bottom_enabled": self.is_scroll_to_bottom_enabled,
                "is_screenshot_enabled": self.is_screenshot_enabled,
            },
            "metadata": {
                "experimental_stealth_mode_enabled": self.is_stealth_mode_enabled,
            },
        }

        if not self.prompt and not self.query:
            self.status = "Either Query or Prompt must be provided."
            raise ValueError(self.status)
        if self.prompt and self.query:
            self.status = "Both Query and Prompt can't be provided at the same time."
            raise ValueError(self.status)

        try:
            # DEBUG: Log the actual request details (without exposing full API key)
            print(f"[AgentQL] 🎯 Making HTTP POST to {endpoint}", flush=True)
            print(f"[AgentQL] 🎯 Headers keys: {list(headers.keys())}", flush=True)
            print(f"[AgentQL] 🎯 X-API-Key header length: {len(headers['X-API-Key'])}, first 10 chars: {headers['X-API-Key'][:10]}", flush=True)
            print(f"[AgentQL] 🎯 Payload keys: {list(payload.keys())}", flush=True)
            logger.info(f"[AgentQL] Making HTTP POST to {endpoint} with API key length {len(headers['X-API-Key'])}")
            
            response = httpx.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
            
            # DEBUG: Log response status
            print(f"[AgentQL] 🎯 HTTP Response status: {response.status_code}", flush=True)
            logger.info(f"[AgentQL] HTTP Response status: {response.status_code}")
            
            response.raise_for_status()

            json = response.json()
            data = Data(result=json["data"], metadata=json["metadata"])

        except httpx.HTTPStatusError as e:
            response = e.response
            if response.status_code == httpx.codes.UNAUTHORIZED:
                self.status = "Please, provide a valid API Key. You can create one at https://dev.agentql.com."
            else:
                try:
                    error_json = response.json()
                    logger.error(
                        f"Failure response: '{response.status_code} {response.reason_phrase}' with body: {error_json}"
                    )
                    msg = error_json["error_info"] if "error_info" in error_json else error_json["detail"]
                except (ValueError, TypeError):
                    msg = f"HTTP {e}."
                self.status = msg
            raise ValueError(self.status) from e

        else:
            self.status = data
            return data
