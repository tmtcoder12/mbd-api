"""Public API request models."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

LanguageCode = Literal["cmn", "eng", "fra", "hin", "jpn", "kor", "spa"]


class WidgetTokenRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    restaurant_id: UUID = Field(alias="restaurantId")
    language: LanguageCode | None = None


class ChatStreamRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(min_length=1)
    restaurant_id: UUID = Field(alias="restaurantId")
    session_token: UUID | None = Field(default=None, alias="sessionToken")
    new_session: bool = Field(default=False, alias="newSession")
    widget_token: str = Field(alias="widgetToken", min_length=1)
    language: LanguageCode | None = None

    @field_validator("message", "widget_token")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
