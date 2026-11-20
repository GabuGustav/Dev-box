from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repository_url: str = Field(min_length=8, max_length=500)
    branch: str = Field(default="main", min_length=1, max_length=255)


class WorkspaceRead(BaseModel):
    id: str
    name: str
    repository_url: str
    branch: str
    status: str
    owner_id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PrincipalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    principal_type: Literal["user", "agent"]
