"""Validated upload manifests shared by API, persistence and storage adapters."""

from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator, model_validator


class UploadFileSpec(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0, le=64 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or "\x00" in value:
            raise ValueError("File names must be relative directory paths without traversal")
        if str(path) != value or value == ".":
            raise ValueError("File names must be normalized relative paths")
        return value


class UploadManifest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    files: list[UploadFileSpec] = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def bounded_series(self) -> "UploadManifest":
        if len({file.name for file in self.files}) != len(self.files):
            raise ValueError("File names must be unique")
        if sum(file.size for file in self.files) > 1024**3:
            raise ValueError("A series must not exceed 1 GiB")
        return self
