"""Constrained provider output, converted to the stable phone storyboard contract."""
import json
import re
from typing import Literal

from pydantic import Field, ValidationError

from .models import StrictModel, StoryPanel, StoryPage, Storyboard


class PlannedSpeech(StrictModel):
    speaker: str = Field(pattern=r"^[^:\r\n]{1,80}$")
    text: str = Field(pattern=r"^[^\r\n]{1,220}$")


class PlannedPanel(StrictModel):
    description: str = Field(pattern=r"^[\s\S]{1,2000}$")
    caption: str = Field(pattern=r"^[\s\S]{0,220}$")
    dialogues: list[PlannedSpeech] = Field(max_length=5)
    bubbleType: Literal["SPEECH", "THOUGHT", "WHISPER", "SHOUT"]
    advanceTrigger: Literal["TAP_TO_NEXT", "AUTO_TIMED", "MANUAL_SWIPE"]
    # Positive durations are safe for all triggers; manual triggers ignore them.
    durationMillis: int = Field(ge=500, le=60000)


class PlannedPage(StrictModel):
    panels: list[PlannedPanel]


class PlannedStoryboard(StrictModel):
    characterBible: str = Field(pattern=r"^[\s\S]{1,6000}$")
    pages: list[PlannedPage]


def output_schema(request):
    schema = PlannedStoryboard.model_json_schema()
    schema["properties"]["pages"].update(minItems=request.targetPageCount, maxItems=request.targetPageCount)
    schema["$defs"]["PlannedPage"]["properties"]["panels"].update(
        minItems=3 if request.autoPanelCount else request.panelsPerPage,
        maxItems=6 if request.autoPanelCount else request.panelsPerPage)
    return schema


def parse_storyboard(text):
    data = json.loads(text)
    pages = data.get("pages", []) if isinstance(data, dict) else []
    structured = isinstance(pages, list) and any(isinstance(p, dict) and "dialogues" in p
        for page in pages if isinstance(page, dict)
        for p in (page.get("panels") if isinstance(page.get("panels"), list) else []))
    if structured:
        board = PlannedStoryboard.model_validate(data)
        return Storyboard(characterBible=board.characterBible, pages=[
            StoryPage(panels=[StoryPanel(description=p.description, caption=p.caption,
                dialogue="\n".join(f"{d.speaker}: {d.text}" for d in p.dialogues), bubbleType=p.bubbleType,
                advanceTrigger=p.advanceTrigger, durationMillis=p.durationMillis) for p in page.panels])
            for page in board.pages])
    # Compatibility with stored/fixture responses in the original phone format.
    return Storyboard.model_validate(data)


def validation_detail(exc, first_page):
    """Report field locations and rule names, never provider text or input values."""
    if isinstance(exc, ValidationError):
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        loc = error["loc"]
        parts = []
        allowed = {"characterBible", "description", "caption", "dialogue", "dialogues", "speaker", "text", "bubbleType", "advanceTrigger", "durationMillis"}
        for i, value in enumerate(loc):
            if isinstance(value, int):
                previous = loc[i-1] if i else ""
                if previous == "pages": parts.append(f"page {first_page + value}")
                elif previous == "panels": parts.append(f"panel {value + 1}")
                elif previous == "dialogues": parts.append(f"speech {value + 1}")
            elif value in allowed:
                parts.append(value)
        rule = {"string_pattern_mismatch": "text format or length limit", "literal_error": "unsupported option",
                "missing": "required field missing", "greater_than_equal": "value below allowed minimum",
                "less_than_equal": "value above allowed maximum", "too_long": "too many items"}.get(error["type"], "invalid field format")
        return ", ".join(parts) + ": " + rule
    if isinstance(exc, json.JSONDecodeError):
        return "planner returned incomplete or invalid JSON"
    # Semantic validator messages are fixed strings with numeric counts only.
    return re.sub(r"\bPage (\d+)", lambda m: f"Page {first_page + int(m[1]) - 1}", str(exc))
