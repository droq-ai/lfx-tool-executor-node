import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import orjson
import pandas as pd
from fastapi import UploadFile
from fastapi.encoders import jsonable_encoder

from lfx.custom import Component
from lfx.inputs import SortableListInput
from lfx.io import DropdownInput, HandleInput, SecretStrInput, StrInput
from lfx.log.logger import logger
from lfx.schema import Data, DataFrame, Message
from lfx.services.deps import get_settings_service, get_storage_service, session_scope
from lfx.template.field.base import Output


class SaveToFileComponent(Component):
    display_name = "Write File"
    description = "Save data to local file, AWS S3, or Google Drive in the selected format."
    documentation: str = "https://docs.langflow.org/components-processing#save-file"
    icon = "file-text"
    name = "SaveToFile"

    # File format options for different storage types
    LOCAL_DATA_FORMAT_CHOICES = ["csv", "excel", "json", "markdown"]
    LOCAL_MESSAGE_FORMAT_CHOICES = ["txt", "json", "markdown"]
    AWS_FORMAT_CHOICES = [
        "txt",
        "json",
        "csv",
        "xml",
        "html",
        "md",
        "yaml",
        "log",
        "tsv",
        "jsonl",
        "parquet",
        "xlsx",
        "zip",
    ]
    GDRIVE_FORMAT_CHOICES = ["txt", "json", "csv", "xlsx", "slides", "docs", "jpg", "mp3"]

    inputs = [
        # Storage location selection
        SortableListInput(
            name="storage_location",
            display_name="Storage Location",
            placeholder="Select Location",
            info="Choose where to save the file.",
            options=[
                {"name": "Local", "icon": "hard-drive"},
                {"name": "AWS", "icon": "Amazon"},
                {"name": "Google Drive", "icon": "google"},
            ],
            real_time_refresh=True,
            limit=1,
        ),
        # Common inputs
        HandleInput(
            name="input",
            display_name="File Content",
            info="The input to save.",
            dynamic=True,
            input_types=["Data", "DataFrame", "Message"],
            required=True,
        ),
        StrInput(
            name="file_name",
            display_name="File Name",
            info="Name file will be saved as (without extension).",
            required=True,
            show=False,
            tool_mode=True,
        ),
        # Format inputs (dynamic based on storage location)
        DropdownInput(
            name="local_format",
            display_name="File Format",
            options=list(dict.fromkeys(LOCAL_DATA_FORMAT_CHOICES + LOCAL_MESSAGE_FORMAT_CHOICES)),
            info="Select the file format for local storage.",
            value="json",
            show=False,
        ),
        DropdownInput(
            name="aws_format",
            display_name="File Format",
            options=AWS_FORMAT_CHOICES,
            info="Select the file format for AWS S3 storage.",
            value="txt",
            show=False,
        ),
        DropdownInput(
            name="gdrive_format",
            display_name="File Format",
            options=GDRIVE_FORMAT_CHOICES,
            info="Select the file format for Google Drive storage.",
            value="txt",
            show=False,
        ),
        # AWS S3 specific inputs
        SecretStrInput(
            name="aws_access_key_id",
            display_name="AWS Access Key ID",
            info="AWS Access key ID.",
            show=False,
            advanced=True,
        ),
        SecretStrInput(
            name="aws_secret_access_key",
            display_name="AWS Secret Key",
            info="AWS Secret Key.",
            show=False,
            advanced=True,
        ),
        StrInput(
            name="bucket_name",
            display_name="S3 Bucket Name",
            info="Enter the name of the S3 bucket.",
            show=False,
            advanced=True,
        ),
        StrInput(
            name="aws_region",
            display_name="AWS Region",
            info="AWS region (e.g., us-east-1, eu-west-1).",
            show=False,
            advanced=True,
        ),
        StrInput(
            name="s3_prefix",
            display_name="S3 Prefix",
            info="Prefix for all files in S3.",
            show=False,
            advanced=True,
        ),
        # Google Drive specific inputs
        SecretStrInput(
            name="service_account_key",
            display_name="GCP Credentials Secret Key",
            info="Your Google Cloud Platform service account JSON key as a secret string (complete JSON content).",
            show=False,
            advanced=True,
        ),
        StrInput(
            name="folder_id",
            display_name="Google Drive Folder ID",
            info=(
                "The Google Drive folder ID where the file will be uploaded. "
                "The folder must be shared with the service account email."
            ),
            show=False,
            advanced=True,
        ),
    ]

    outputs = [Output(display_name="File Path", name="message", method="save_to_file")]

    def update_build_config(self, build_config, field_value, field_name=None):
        """Update build configuration to show/hide fields based on storage location selection."""
        if field_name != "storage_location":
            return build_config

        # Extract selected storage location
        selected = [location["name"] for location in field_value] if isinstance(field_value, list) else []

        # Hide all dynamic fields first
        dynamic_fields = [
            "file_name",  # Common fields (input is always visible)
            "local_format",
            "aws_format",
            "gdrive_format",
            "aws_access_key_id",
            "aws_secret_access_key",
            "bucket_name",
            "aws_region",
            "s3_prefix",
            "service_account_key",
            "folder_id",
        ]

        for f_name in dynamic_fields:
            if f_name in build_config:
                build_config[f_name]["show"] = False

        # Show fields based on selected storage location
        if len(selected) == 1:
            location = selected[0]

            # Show file_name when any storage location is selected (input is always visible)
            if "file_name" in build_config:
                build_config["file_name"]["show"] = True

            if location == "Local":
                if "local_format" in build_config:
                    build_config["local_format"]["show"] = True

            elif location == "AWS":
                aws_fields = [
                    "aws_format",
                    "aws_access_key_id",
                    "aws_secret_access_key",
                    "bucket_name",
                    "aws_region",
                    "s3_prefix",
                ]
                for f_name in aws_fields:
                    if f_name in build_config:
                        build_config[f_name]["show"] = True

            elif location == "Google Drive":
                gdrive_fields = ["gdrive_format", "service_account_key", "folder_id"]
                for f_name in gdrive_fields:
                    if f_name in build_config:
                        build_config[f_name]["show"] = True

        return build_config

    async def save_to_file(self) -> Message:
        """Save the input to a file and upload it, returning a confirmation message."""
        # Validate inputs
        if not self.file_name:
            msg = "File name must be provided."
            raise ValueError(msg)
        if not self._get_input_type():
            msg = "Input type is not set."
            raise ValueError(msg)

        # Get selected storage location
        storage_location = self._get_selected_storage_location()
        if not storage_location:
            msg = "Storage location must be selected."
            raise ValueError(msg)

        # Route to appropriate save method based on storage location
        if storage_location == "Local":
            return await self._save_to_local()
        if storage_location == "AWS":
            return await self._save_to_aws()
        if storage_location == "Google Drive":
            return await self._save_to_google_drive()
        msg = f"Unsupported storage location: {storage_location}"
        raise ValueError(msg)

    def _get_input_type(self) -> str:
        """Determine the input type based on the provided input."""
        # Use exact type checking (type() is) instead of isinstance() to avoid inheritance issues.
        # Since Message inherits from Data, isinstance(message, Data) would return True for Message objects,
        # causing Message inputs to be incorrectly identified as Data type.
        if type(self.input) is DataFrame:
            return "DataFrame"
        if type(self.input) is Message:
            return "Message"
        if type(self.input) is Data:
            return "Data"
        msg = f"Unsupported input type: {type(self.input)}"
        raise ValueError(msg)

    def _get_default_format(self) -> str:
        """Return the default file format based on input type."""
        if self._get_input_type() == "DataFrame":
            return "csv"
        if self._get_input_type() == "Data":
            return "json"
        if self._get_input_type() == "Message":
            return "json"
        return "json"  # Fallback

    def _adjust_file_path_with_format(self, path: Path, fmt: str) -> Path:
        """Adjust the file path to include the correct extension."""
        file_extension = path.suffix.lower().lstrip(".")
        if fmt == "excel":
            return Path(f"{path}.xlsx").expanduser() if file_extension not in ["xlsx", "xls"] else path
        return Path(f"{path}.{fmt}").expanduser() if file_extension != fmt else path

    async def _upload_file(self, file_path: Path) -> None:
        """Upload the saved file using the upload_user_file service.
        
        If running in executor node (no backend access), skip upload and just save locally.
        """
        # Ensure the file exists
        if not file_path.exists():
            msg = f"File not found: {file_path}"
            raise FileNotFoundError(msg)

        # Try to upload to backend, but skip if running in executor node
        try:
            from langflow.api.v2.files import upload_user_file
            from langflow.services.database.models.user.crud import get_user_by_id
            from langflow.services.deps import session_scope, get_storage_service, get_settings_service
            from fastapi import UploadFile
        except ImportError:
            # Running in executor node - skip upload, file is already saved locally
            logger.debug(f"[EXECUTOR] Skipping file upload (executor node), file saved at: {file_path}")
            return

        # Upload the file to backend storage
        try:
            with file_path.open("rb") as f:
                async with session_scope() as db:
                    if not self.user_id:
                        msg = "User ID is required for file saving."
                        raise ValueError(msg)
                    current_user = await get_user_by_id(db, self.user_id)

                    await upload_user_file(
                        file=UploadFile(filename=file_path.name, file=f, size=file_path.stat().st_size),
                        session=db,
                        current_user=current_user,
                        storage_service=get_storage_service(),
                        settings_service=get_settings_service(),
                    )
        except Exception as e:
            # If upload fails, log but don't fail - file is already saved locally
            logger.warning(f"Failed to upload file to backend storage: {e}. File saved locally at: {file_path}")

    def _save_dataframe(self, dataframe: DataFrame, path: Path, fmt: str) -> str:
        """Save a DataFrame to the specified file format."""
        file_exists = path.exists()
        print(f"[SaveFile] 💾 Writing DataFrame to {fmt} file: exists={file_exists}, mode={'append rows' if file_exists else 'create'}", flush=True)
        print(f"[SaveFile] 💾 DataFrame shape: {dataframe.shape}", flush=True)
        
        if fmt == "csv":
            if file_exists:
                try:
                    existing_df = pd.read_csv(path)
                    print(f"[SaveFile] 🔄 Read existing CSV with {len(existing_df)} row(s)", flush=True)
                    combined_df = pd.concat([existing_df, dataframe], ignore_index=True)
                    combined_df.to_csv(path, index=False)
                    print(f"[SaveFile] 💾 ✅ Appended rows to CSV (now {len(combined_df)} rows): {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing CSV: {e}, creating new file", flush=True)
                    dataframe.to_csv(path, index=False)
                    print(f"[SaveFile] 💾 ✅ Created new CSV file: {path}", flush=True)
            else:
                dataframe.to_csv(path, index=False)
                print(f"[SaveFile] 💾 ✅ Created new CSV file: {path}", flush=True)
        elif fmt == "excel":
            if file_exists:
                try:
                    existing_df = pd.read_excel(path, engine="openpyxl")
                    print(f"[SaveFile] 🔄 Read existing Excel with {len(existing_df)} row(s)", flush=True)
                    combined_df = pd.concat([existing_df, dataframe], ignore_index=True)
                    combined_df.to_excel(path, index=False, engine="openpyxl")
                    print(f"[SaveFile] 💾 ✅ Appended rows to Excel (now {len(combined_df)} rows): {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing Excel: {e}, creating new file", flush=True)
                    dataframe.to_excel(path, index=False, engine="openpyxl")
                    print(f"[SaveFile] 💾 ✅ Created new Excel file: {path}", flush=True)
            else:
                dataframe.to_excel(path, index=False, engine="openpyxl")
                print(f"[SaveFile] 💾 ✅ Created new Excel file: {path}", flush=True)
        elif fmt == "json":
            if file_exists:
                try:
                    with path.open("r", encoding="utf-8") as f:
                        existing_content = f.read().strip()
                    
                    json_array = []
                    if existing_content.startswith("["):
                        json_array = json.loads(existing_content)
                        print(f"[SaveFile] 🔄 Read existing JSON array with {len(json_array)} item(s)", flush=True)
                    elif existing_content.startswith("{"):
                        json_array = [json.loads(existing_content)]
                        print(f"[SaveFile] 🔄 Converted single JSON object to array", flush=True)
                    
                    # Convert DataFrame to list of dicts and append
                    df_records = dataframe.to_dict(orient="records")
                    json_array.extend(df_records)
                    
                    with path.open("w", encoding="utf-8") as f:
                        f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                    print(f"[SaveFile] 💾 ✅ Appended DataFrame rows to JSON array (now {len(json_array)} items): {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing JSON: {e}, creating new file", flush=True)
                    dataframe.to_json(path, orient="records", indent=2)
                    print(f"[SaveFile] 💾 ✅ Created new JSON file: {path}", flush=True)
            else:
                dataframe.to_json(path, orient="records", indent=2)
                print(f"[SaveFile] 💾 ✅ Created new JSON file: {path}", flush=True)
        elif fmt == "markdown":
            if file_exists:
                try:
                    with path.open("r", encoding="utf-8") as f:
                        existing_content = f.read()
                    
                    new_markdown = dataframe.to_markdown(index=False)
                    with path.open("a", encoding="utf-8") as f:
                        if existing_content and not existing_content.endswith("\n"):
                            f.write("\n")
                        f.write(f"\n{new_markdown}\n")
                    print(f"[SaveFile] 💾 ✅ Appended markdown table to existing file: {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing markdown: {e}, creating new file", flush=True)
                    path.write_text(dataframe.to_markdown(index=False), encoding="utf-8")
                    print(f"[SaveFile] 💾 ✅ Created new markdown file: {path}", flush=True)
            else:
                path.write_text(dataframe.to_markdown(index=False), encoding="utf-8")
                print(f"[SaveFile] 💾 ✅ Created new markdown file: {path}", flush=True)
        else:
            msg = f"Unsupported DataFrame format: {fmt}"
            raise ValueError(msg)
        return f"DataFrame saved successfully as '{path}'"

    def _save_data(self, data: Data, path: Path, fmt: str) -> str:
        """Save a Data object to the specified file format."""
        print(f"[SaveFile] 💾 _save_data called: format={fmt}, path={path}", flush=True)
        
        # CRITICAL: Skip if content is empty (don't fail, just skip writing)
        # Check if Data object has meaningful content
        text_content = getattr(data, "text", None) or (data.data.get("text") if isinstance(data.data, dict) else None)
        data_dict = data.data if isinstance(data.data, dict) else {}
        
        print(f"[SaveFile] 💾 Data.text: {repr(text_content)[:200] if text_content else 'None'}", flush=True)
        print(f"[SaveFile] 💾 Data.data: {repr(data_dict)[:200] if data_dict else 'None'}", flush=True)
        
        # Check if text is empty or only whitespace
        is_text_empty = not text_content or (isinstance(text_content, str) and text_content.strip() == "")
        
        # Check if data dict is empty or only contains empty values
        is_data_empty = not data_dict or all(
            not v or (isinstance(v, str) and v.strip() == "") 
            for v in data_dict.values() 
            if v is not None
        )
        
        if is_text_empty and is_data_empty:
            print(
                f"[SaveFile] ⚠️ SKIP: Data content is empty, skipping write. data.data={repr(data.data)}, text={repr(text_content)}",
                flush=True,
            )
            logger.info(f"[SaveFile] Skipping write to '{path}' - content is empty")
            setattr(self, "_skipped_last_write", True)
            return f"Skipped writing empty content to '{path}'"
        
        file_exists = path.exists()
        print(f"[SaveFile] 💾 Writing Data to {fmt} file: exists={file_exists}", flush=True)
        
        if fmt == "csv":
            file_exists = path.exists()
            new_df = pd.DataFrame([data.data])  # Convert single data dict to DataFrame
            print(f"[SaveFile] 💾 Writing Data to csv file: exists={file_exists}, mode={'append rows' if file_exists else 'create'}", flush=True)
            print(f"[SaveFile] 💾 CSV data to add: {data.data}", flush=True)
            
            if file_exists:
                try:
                    # Read existing CSV
                    existing_df = pd.read_csv(path)
                    print(f"[SaveFile] 🔄 Read existing CSV with {len(existing_df)} row(s)", flush=True)
                    # Append new row(s)
                    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                    combined_df.to_csv(path, index=False)
                    print(f"[SaveFile] 💾 ✅ Appended row to CSV (now {len(combined_df)} rows): {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing CSV: {e}, creating new file", flush=True)
                    # If reading fails, create new file
                    new_df.to_csv(path, index=False)
                    print(f"[SaveFile] 💾 ✅ Created new CSV file: {path}", flush=True)
            else:
                new_df.to_csv(path, index=False)
                print(f"[SaveFile] 💾 ✅ Created new CSV file: {path}", flush=True)
        elif fmt == "excel":
            file_exists = path.exists()
            new_df = pd.DataFrame([data.data])  # Convert single data dict to DataFrame
            print(f"[SaveFile] 💾 Writing Data to excel file: exists={file_exists}, mode={'append rows' if file_exists else 'create'}", flush=True)
            print(f"[SaveFile] 💾 Excel data to add: {data.data}", flush=True)
            
            if file_exists:
                try:
                    # Read existing Excel file
                    existing_df = pd.read_excel(path, engine="openpyxl")
                    print(f"[SaveFile] 🔄 Read existing Excel with {len(existing_df)} row(s)", flush=True)
                    # Append new row(s)
                    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                    combined_df.to_excel(path, index=False, engine="openpyxl")
                    print(f"[SaveFile] 💾 ✅ Appended row to Excel (now {len(combined_df)} rows): {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing Excel: {e}, creating new file", flush=True)
                    # If reading fails, create new file
                    new_df.to_excel(path, index=False, engine="openpyxl")
                    print(f"[SaveFile] 💾 ✅ Created new Excel file: {path}", flush=True)
            else:
                new_df.to_excel(path, index=False, engine="openpyxl")
                print(f"[SaveFile] 💾 ✅ Created new Excel file: {path}", flush=True)
        elif fmt == "json":
            file_exists = path.exists()
            # For JSON, use JSON array format for valid JSON file
            new_item = jsonable_encoder(data.data)
            print(f"[SaveFile] 💾 Writing Data to json file: exists={file_exists}, mode={'append to array' if file_exists else 'create array'}", flush=True)
            print(f"[SaveFile] 💾 JSON content to add: {json.dumps(new_item)[:200]}", flush=True)
            
            if file_exists:
                # Read existing file and parse as JSON array
                try:
                    with path.open("r", encoding="utf-8") as f:
                        existing_content = f.read().strip()
                    
                    json_array = []
                    
                    # Try to parse as JSON array
                    if existing_content.startswith("["):
                        try:
                            json_array = json.loads(existing_content)
                            print(f"[SaveFile] 🔄 Read existing JSON array with {len(json_array)} item(s)", flush=True)
                        except json.JSONDecodeError as e:
                            print(f"[SaveFile] ⚠️ Error parsing JSON array: {e}, creating new array", flush=True)
                            json_array = []
                    
                    # Try to parse as single JSON object (convert to array)
                    elif existing_content.startswith("{"):
                        try:
                            obj = json.loads(existing_content)
                            json_array = [obj]
                            print(f"[SaveFile] 🔄 Converted single JSON object to array", flush=True)
                        except json.JSONDecodeError as e:
                            print(f"[SaveFile] ⚠️ Error parsing JSON object: {e}, creating new array", flush=True)
                            json_array = []
                    
                    # Try to parse as JSONL (one object per line)
                    else:
                        lines = existing_content.split("\n")
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                json_array.append(obj)
                            except json.JSONDecodeError:
                                pass
                        if json_array:
                            print(f"[SaveFile] 🔄 Converted JSONL format to array with {len(json_array)} item(s)", flush=True)
                    
                    # Add new item to array
                    json_array.append(new_item)
                    
                    # Write back as formatted JSON array
                    with path.open("w", encoding="utf-8") as f:
                        f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                    print(f"[SaveFile] 💾 ✅ Appended to JSON array (now {len(json_array)} items): {path}", flush=True)
                
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing file: {e}, creating new array", flush=True)
                    # Create new array with the item
                    json_array = [new_item]
                    with path.open("w", encoding="utf-8") as f:
                        f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                    print(f"[SaveFile] 💾 ✅ Created new JSON array file: {path}", flush=True)
            else:
                # Create new file with JSON array format
                json_array = [new_item]
                with path.open("w", encoding="utf-8") as f:
                    f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                print(f"[SaveFile] 💾 ✅ Created new JSON array file with Data: {path}", flush=True)
        elif fmt == "markdown":
            file_exists = path.exists()
            new_df = pd.DataFrame([data.data])  # Convert single data dict to DataFrame
            new_markdown = new_df.to_markdown(index=False)
            print(f"[SaveFile] 💾 Writing Data to markdown file: exists={file_exists}, mode={'append' if file_exists else 'create'}", flush=True)
            print(f"[SaveFile] 💾 Markdown data to add: {data.data}", flush=True)
            
            if file_exists:
                # Read existing markdown and append new table
                try:
                    with path.open("r", encoding="utf-8") as f:
                        existing_content = f.read()
                    
                    # Append new markdown table with separator
                    with path.open("a", encoding="utf-8") as f:
                        # Ensure file ends with newline
                        if existing_content and not existing_content.endswith("\n"):
                            f.write("\n")
                        f.write(f"\n{new_markdown}\n")
                    print(f"[SaveFile] 💾 ✅ Appended markdown table to existing file: {path}", flush=True)
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing markdown: {e}, creating new file", flush=True)
                    path.write_text(f"{new_markdown}\n", encoding="utf-8")
                    print(f"[SaveFile] 💾 ✅ Created new markdown file: {path}", flush=True)
            else:
                path.write_text(f"{new_markdown}\n", encoding="utf-8")
                print(f"[SaveFile] 💾 ✅ Created new markdown file: {path}", flush=True)
        else:
            msg = f"Unsupported Data format: {fmt}"
            raise ValueError(msg)
        return f"Data saved successfully as '{path}'"

    async def _save_message(self, message: Message, path: Path, fmt: str) -> str:
        """Save a Message to the specified file format, handling async iterators."""
        print(f"[SaveFile] 💾 _save_message called: format={fmt}, path={path}", flush=True)
        print(f"[SaveFile] 💾 message.text type: {type(message.text).__name__}, value={repr(message.text)[:200]}", flush=True)
        
        content = ""
        if message.text is None:
            content = ""
            print(f"[SaveFile] 💾 message.text is None, content=''", flush=True)
        elif isinstance(message.text, AsyncIterator):
            print(f"[SaveFile] 💾 message.text is AsyncIterator, extracting...", flush=True)
            async for item in message.text:
                content += str(item) + " "
            content = content.strip()
            print(f"[SaveFile] 💾 Extracted from AsyncIterator: length={len(content)}, content={repr(content)[:200]}", flush=True)
        elif isinstance(message.text, Iterator):
            print(f"[SaveFile] 💾 message.text is Iterator, extracting...", flush=True)
            content = " ".join(str(item) for item in message.text)
            print(f"[SaveFile] 💾 Extracted from Iterator: length={len(content)}, content={repr(content)[:200]}", flush=True)
        else:
            content = str(message.text)
            print(f"[SaveFile] 💾 Converted to string: length={len(content)}, content={repr(content)[:200]}", flush=True)

        # CRITICAL: Skip if content is empty (don't fail, just skip writing)
        # This prevents writing empty files which indicates a problem in the workflow
        print(f"[SaveFile] 💾 Final content: type={type(content).__name__}, length={len(content) if isinstance(content, str) else 'N/A'}, value={repr(content)[:200]}", flush=True)
        if not content or (isinstance(content, str) and content.strip() == ""):
            print(
                f"[SaveFile] ⚠️ SKIP: Content is empty, skipping write. message.text={repr(message.text)}, content={repr(content)}",
                flush=True,
            )
            logger.info(f"[SaveFile] Skipping write to '{path}' - content is empty")
            setattr(self, "_skipped_last_write", True)
            return f"Skipped writing empty content to '{path}'"

        if fmt == "txt":
            # Append mode: if file exists, append with newline; otherwise create new
            file_exists = path.exists()
            print(f"[SaveFile] 💾 Writing to txt file: exists={file_exists}, mode={'append' if file_exists else 'create'}", flush=True)
            if file_exists:
                # Ensure file ends with newline before appending
                with path.open("r+", encoding="utf-8") as f:
                    f.seek(0, 2)  # Go to end
                    file_size = f.tell()
                    if file_size > 0:
                        f.seek(-1, 2)  # Go back one byte
                        last_char = f.read(1)
                        if last_char != "\n":
                            f.write("\n")  # Add newline if missing
                    # Append new content
                    f.write(f"{content}\n")
                print(f"[SaveFile] 💾 ✅ Appended content to existing file: {path}", flush=True)
            else:
                path.write_text(f"{content}\n", encoding="utf-8")
                print(f"[SaveFile] 💾 ✅ Created new file with content: {path}", flush=True)
        elif fmt == "json":
            # For JSON, use JSON array format for valid JSON file
            file_exists = path.exists()
            new_item = {"message": content}
            print(f"[SaveFile] 💾 Writing to json file: exists={file_exists}, mode={'append to array' if file_exists else 'create array'}", flush=True)
            print(f"[SaveFile] 💾 JSON content to add: {json.dumps(new_item)[:200]}", flush=True)
            
            if file_exists:
                # Read existing file and parse as JSON array
                try:
                    with path.open("r", encoding="utf-8") as f:
                        existing_content = f.read().strip()
                    
                    json_array = []
                    
                    # Try to parse as JSON array
                    if existing_content.startswith("["):
                        try:
                            json_array = json.loads(existing_content)
                            print(f"[SaveFile] 🔄 Read existing JSON array with {len(json_array)} item(s)", flush=True)
                        except json.JSONDecodeError as e:
                            print(f"[SaveFile] ⚠️ Error parsing JSON array: {e}, creating new array", flush=True)
                            json_array = []
                    
                    # Try to parse as single JSON object (convert to array)
                    elif existing_content.startswith("{"):
                        try:
                            obj = json.loads(existing_content)
                            json_array = [obj]
                            print(f"[SaveFile] 🔄 Converted single JSON object to array", flush=True)
                        except json.JSONDecodeError:
                            print(f"[SaveFile] ⚠️ Error parsing JSON object: {e}, creating new array", flush=True)
                            json_array = []
                    
                    # Try to parse as JSONL (one object per line)
                    else:
                        lines = existing_content.split("\n")
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                json_array.append(obj)
                            except json.JSONDecodeError:
                                pass
                        if json_array:
                            print(f"[SaveFile] 🔄 Converted JSONL format to array with {len(json_array)} item(s)", flush=True)
                    
                    # Add new item to array
                    json_array.append(new_item)
                    
                    # Write back as formatted JSON array
                    with path.open("w", encoding="utf-8") as f:
                        f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                    print(f"[SaveFile] 💾 ✅ Appended to JSON array (now {len(json_array)} items): {path}", flush=True)
                
                except Exception as e:
                    print(f"[SaveFile] ⚠️ Error reading existing file: {e}, creating new array", flush=True)
                    # Create new array with the item
                    json_array = [new_item]
                    with path.open("w", encoding="utf-8") as f:
                        f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                    print(f"[SaveFile] 💾 ✅ Created new JSON array file: {path}", flush=True)
            else:
                # Create new file with JSON array format
                json_array = [new_item]
                with path.open("w", encoding="utf-8") as f:
                    f.write(json.dumps(json_array, indent=2, ensure_ascii=False))
                print(f"[SaveFile] 💾 ✅ Created new JSON array file with content: {path}", flush=True)
        elif fmt == "markdown":
            # Append mode for markdown
            file_exists = path.exists()
            print(f"[SaveFile] 💾 Writing to markdown file: exists={file_exists}, mode={'append' if file_exists else 'create'}", flush=True)
            if file_exists:
                # Ensure file ends with newline before appending
                with path.open("r+", encoding="utf-8") as f:
                    f.seek(0, 2)  # Go to end
                    file_size = f.tell()
                    if file_size > 0:
                        f.seek(-1, 2)  # Go back one byte
                        last_char = f.read(1)
                        if last_char != "\n":
                            f.write("\n")  # Add newline if missing
                    # Append new content with separator
                    f.write(f"\n**Message:**\n\n{content}\n")
                print(f"[SaveFile] 💾 ✅ Appended markdown content to existing file: {path}", flush=True)
            else:
                path.write_text(f"**Message:**\n\n{content}\n", encoding="utf-8")
                print(f"[SaveFile] 💾 ✅ Created new markdown file with content: {path}", flush=True)
        else:
            msg = f"Unsupported Message format: {fmt}"
            raise ValueError(msg)
        return f"Message saved successfully as '{path}'"

    def _get_selected_storage_location(self) -> str:
        """Get the selected storage location from the SortableListInput."""
        if hasattr(self, "storage_location") and self.storage_location:
            if isinstance(self.storage_location, list) and len(self.storage_location) > 0:
                return self.storage_location[0].get("name", "")
            if isinstance(self.storage_location, dict):
                return self.storage_location.get("name", "")
        return ""

    def _get_file_format_for_location(self, location: str) -> str:
        """Get the appropriate file format based on storage location."""
        if location == "Local":
            return getattr(self, "local_format", None) or self._get_default_format()
        if location == "AWS":
            return getattr(self, "aws_format", "txt")
        if location == "Google Drive":
            return getattr(self, "gdrive_format", "txt")
        return self._get_default_format()

    async def _save_to_local(self) -> Message:
        """Save file to local storage (original functionality)."""
        file_format = self._get_file_format_for_location("Local")
        input_type = self._get_input_type()

        # Log what we're receiving
        print(f"[SaveFile] 📥 RECEIVED INPUT: type={input_type}, format={file_format}, file_name={self.file_name}", flush=True)
        print(f"[SaveFile] 📥 Input object: {type(self.input).__name__}", flush=True)
        
        # Log the actual content based on type
        if input_type == "Message":
            msg_text = getattr(self.input, "text", None)
            msg_data = getattr(self.input, "data", None)
            print(f"[SaveFile] 📥 Message.text: type={type(msg_text).__name__}, value={repr(msg_text)[:200]}", flush=True)
            print(f"[SaveFile] 📥 Message.data: {repr(msg_data)[:200] if msg_data else 'None'}", flush=True)
        elif input_type == "Data":
            data_text = getattr(self.input, "text", None)
            data_dict = getattr(self.input, "data", None)
            print(f"[SaveFile] 📥 Data.text: {repr(data_text)[:200] if data_text else 'None'}", flush=True)
            print(f"[SaveFile] 📥 Data.data: {repr(data_dict)[:200] if data_dict else 'None'}", flush=True)
        elif input_type == "DataFrame":
            print(f"[SaveFile] 📥 DataFrame shape: {self.input.shape if hasattr(self.input, 'shape') else 'N/A'}", flush=True)
            print(f"[SaveFile] 📥 DataFrame preview: {str(self.input.head())[:200] if hasattr(self.input, 'head') else 'N/A'}", flush=True)

        # Validate file format based on input type
        allowed_formats = (
            self.LOCAL_MESSAGE_FORMAT_CHOICES if input_type == "Message" else self.LOCAL_DATA_FORMAT_CHOICES
        )
        if file_format not in allowed_formats:
            msg = f"Invalid file format '{file_format}' for {input_type}. Allowed: {allowed_formats}"
            raise ValueError(msg)

        # Prepare file path
        file_path = Path(self.file_name).expanduser()
        if not file_path.parent.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path = self._adjust_file_path_with_format(file_path, file_format)
        
        print(f"[SaveFile] 📁 Target file path: {file_path}", flush=True)
        print(f"[SaveFile] 📁 File exists: {file_path.exists()}", flush=True)

        # Track whether we skipped writing due to empty content
        setattr(self, "_skipped_last_write", False)

        # Save the input to file based on type
        if input_type == "DataFrame":
            confirmation = self._save_dataframe(self.input, file_path, file_format)
        elif input_type == "Data":
            confirmation = self._save_data(self.input, file_path, file_format)
        elif input_type == "Message":
            confirmation = await self._save_message(self.input, file_path, file_format)
        else:
            msg = f"Unsupported input type: {input_type}"
            raise ValueError(msg)
        
        print(f"[SaveFile] ✅ Save result: {confirmation}", flush=True)

        # Upload the saved file only if we actually wrote content
        if not getattr(self, "_skipped_last_write", False):
            await self._upload_file(file_path)
        else:
            print("[SaveFile] ⏭️ Upload skipped because no content was written.", flush=True)

        # Return the final file path and confirmation message
        final_path = Path.cwd() / file_path if not file_path.is_absolute() else file_path
        return Message(text=f"{confirmation} at {final_path}")

    async def _save_to_aws(self) -> Message:
        """Save file to AWS S3 using S3 functionality."""
        # Validate AWS credentials
        if not getattr(self, "aws_access_key_id", None):
            msg = "AWS Access Key ID is required for S3 storage"
            raise ValueError(msg)
        if not getattr(self, "aws_secret_access_key", None):
            msg = "AWS Secret Key is required for S3 storage"
            raise ValueError(msg)
        if not getattr(self, "bucket_name", None):
            msg = "S3 Bucket Name is required for S3 storage"
            raise ValueError(msg)

        # Use S3 upload functionality
        try:
            import boto3
        except ImportError as e:
            msg = "boto3 is not installed. Please install it using `uv pip install boto3`."
            raise ImportError(msg) from e

        # Create S3 client
        client_config = {
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
        }

        if hasattr(self, "aws_region") and self.aws_region:
            client_config["region_name"] = self.aws_region

        s3_client = boto3.client("s3", **client_config)

        # Extract content
        content = self._extract_content_for_upload()
        file_format = self._get_file_format_for_location("AWS")

        # Generate file path
        file_path = f"{self.file_name}.{file_format}"
        if hasattr(self, "s3_prefix") and self.s3_prefix:
            file_path = f"{self.s3_prefix.rstrip('/')}/{file_path}"

        # Create temporary file
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=f".{file_format}", delete=False) as temp_file:
            temp_file.write(content)
            temp_file_path = temp_file.name

        try:
            # Upload to S3
            s3_client.upload_file(temp_file_path, self.bucket_name, file_path)
            s3_url = f"s3://{self.bucket_name}/{file_path}"
            return Message(text=f"File successfully uploaded to {s3_url}")
        finally:
            # Clean up temp file
            if Path(temp_file_path).exists():
                Path(temp_file_path).unlink()

    async def _save_to_google_drive(self) -> Message:
        """Save file to Google Drive using Google Drive functionality."""
        # Validate Google Drive credentials
        if not getattr(self, "service_account_key", None):
            msg = "GCP Credentials Secret Key is required for Google Drive storage"
            raise ValueError(msg)
        if not getattr(self, "folder_id", None):
            msg = "Google Drive Folder ID is required for Google Drive storage"
            raise ValueError(msg)

        # Use Google Drive upload functionality
        try:
            import json
            import tempfile

            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            msg = "Google API client libraries are not installed. Please install them."
            raise ImportError(msg) from e

        # Parse credentials
        try:
            credentials_dict = json.loads(self.service_account_key)
        except json.JSONDecodeError as e:
            msg = f"Invalid JSON in service account key: {e!s}"
            raise ValueError(msg) from e

        # Create Google Drive service
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict, scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        drive_service = build("drive", "v3", credentials=credentials)

        # Extract content and format
        content = self._extract_content_for_upload()
        file_format = self._get_file_format_for_location("Google Drive")

        # Handle special Google Drive formats
        if file_format in ["slides", "docs"]:
            return await self._save_to_google_apps(drive_service, content, file_format)

        # Create temporary file
        file_path = f"{self.file_name}.{file_format}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=f".{file_format}", delete=False) as temp_file:
            temp_file.write(content)
            temp_file_path = temp_file.name

        try:
            # Upload to Google Drive
            file_metadata = {"name": file_path, "parents": [self.folder_id]}
            media = MediaFileUpload(temp_file_path, resumable=True)

            uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()

            file_id = uploaded_file.get("id")
            file_url = f"https://drive.google.com/file/d/{file_id}/view"
            return Message(text=f"File successfully uploaded to Google Drive: {file_url}")
        finally:
            # Clean up temp file
            if Path(temp_file_path).exists():
                Path(temp_file_path).unlink()

    async def _save_to_google_apps(self, drive_service, content: str, app_type: str) -> Message:
        """Save content to Google Apps (Slides or Docs)."""
        import time

        if app_type == "slides":
            from googleapiclient.discovery import build

            slides_service = build("slides", "v1", credentials=drive_service._http.credentials)

            file_metadata = {
                "name": self.file_name,
                "mimeType": "application/vnd.google-apps.presentation",
                "parents": [self.folder_id],
            }

            created_file = drive_service.files().create(body=file_metadata, fields="id").execute()
            presentation_id = created_file["id"]

            time.sleep(2)  # Wait for file to be available  # noqa: ASYNC251

            presentation = slides_service.presentations().get(presentationId=presentation_id).execute()
            slide_id = presentation["slides"][0]["objectId"]

            # Add content to slide
            requests = [
                {
                    "createShape": {
                        "objectId": "TextBox_01",
                        "shapeType": "TEXT_BOX",
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "height": {"magnitude": 3000000, "unit": "EMU"},
                                "width": {"magnitude": 6000000, "unit": "EMU"},
                            },
                            "transform": {
                                "scaleX": 1,
                                "scaleY": 1,
                                "translateX": 1000000,
                                "translateY": 1000000,
                                "unit": "EMU",
                            },
                        },
                    }
                },
                {"insertText": {"objectId": "TextBox_01", "insertionIndex": 0, "text": content}},
            ]

            slides_service.presentations().batchUpdate(
                presentationId=presentation_id, body={"requests": requests}
            ).execute()
            file_url = f"https://docs.google.com/presentation/d/{presentation_id}/edit"

        elif app_type == "docs":
            from googleapiclient.discovery import build

            docs_service = build("docs", "v1", credentials=drive_service._http.credentials)

            file_metadata = {
                "name": self.file_name,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [self.folder_id],
            }

            created_file = drive_service.files().create(body=file_metadata, fields="id").execute()
            document_id = created_file["id"]

            time.sleep(2)  # Wait for file to be available  # noqa: ASYNC251

            # Add content to document
            requests = [{"insertText": {"location": {"index": 1}, "text": content}}]
            docs_service.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
            file_url = f"https://docs.google.com/document/d/{document_id}/edit"

        return Message(text=f"File successfully created in Google {app_type.title()}: {file_url}")

    def _extract_content_for_upload(self) -> str:
        """Extract content from input for upload to cloud services."""
        if self._get_input_type() == "DataFrame":
            return self.input.to_csv(index=False)
        if self._get_input_type() == "Data":
            if hasattr(self.input, "data") and self.input.data:
                if isinstance(self.input.data, dict):
                    import json

                    return json.dumps(self.input.data, indent=2, ensure_ascii=False)
                return str(self.input.data)
            return str(self.input)
        if self._get_input_type() == "Message":
            return str(self.input.text) if self.input.text else str(self.input)
        return str(self.input)
