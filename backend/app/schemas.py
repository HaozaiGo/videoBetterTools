from typing import Any, Literal

from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str
    name: str
    role: str = "user"
    initialCredits: int = 0


class UserRecharge(BaseModel):
    credits: int


class TaskCreate(BaseModel):
    toolSlug: str
    inputAssetId: str
    params: dict[str, Any] = {}


class AssetComplete(BaseModel):
    assetId: str
    kind: str = "video"
    originalName: str
    mimeType: str = "application/octet-stream"
    storageKey: str
    sizeBytes: int = 0
    durationSeconds: int = 0


class RechargeCreate(BaseModel):
    credits: int


class ProviderCallback(BaseModel):
    providerJobId: str
    status: Literal["processing", "succeeded", "failed"]
    callbackId: str | None = None
    outputUrl: str | None = None
    outputStorageKey: str | None = None
    outputMimeType: str | None = None
    outputSizeBytes: int | None = None
    chargedCredits: int | None = None
    errorCode: str | None = None
