from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttachmentReference(StrictModel):
    id: UUID
    useForAi: bool = False
    label: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)
    role: str = Field(default="DOCUMENT", pattern="^(CHARACTER|LOCATION|DOCUMENT|STYLE)$")


class GenerationRequest(StrictModel):
    requestId: UUID
    rawScriptInput: str = Field(min_length=1, max_length=200000)
    selectedArtStyle: str = Field(min_length=1, max_length=100)
    targetPageCount: int = Field(ge=1, le=35, strict=True)
    panelsPerPage: int = Field(default=4, ge=3, le=6, strict=True)
    layoutStyle: str = Field(default="GRID", pattern="^(GRID|CINEMATIC)$")
    continuityNotes: str = Field(default="", max_length=6000)

    requireArtworkApproval: bool = False
    autoPanelCount: bool = False
    analyzeBalloonSpace: bool = False
    compareCharacterFaces: bool = False

    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=20)

    @field_validator("attachments")
    @classmethod
    def unique_attachments(cls, value):
        if len({v.id for v in value}) != len(value):
            raise ValueError("Duplicate attachment IDs")
        return value

    @field_validator("rawScriptInput", "selectedArtStyle")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class StoryPanel(StrictModel):
    description: str
    caption: str
    dialogue: str
    bubbleType: str
    advanceTrigger: str
    durationMillis: int


class StoryPage(StrictModel):
    panels: list[StoryPanel]


class Storyboard(StrictModel):
    characterBible: str
    pages: list[StoryPage]


def validate_storyboard(board: Storyboard, pages: int, panels_per_page: int = 4, auto: bool = False):
    if len(board.pages) != pages:
        raise ValueError(f"Expected {pages} pages, received {len(board.pages)}")
    if not board.characterBible.strip() or len(board.characterBible) > 6000:
        raise ValueError("Invalid continuity guide")
    for page_index, page in enumerate(board.pages, start=1):
        if (auto and not 3 <= len(page.panels) <= 6) or (not auto and len(page.panels) != panels_per_page):
            raise ValueError(f"Page {page_index}: expected {'3-6' if auto else panels_per_page} panels, received {len(page.panels)}")
        for panel_index, panel in enumerate(page.panels, start=1):
            if not panel.description.strip() or len(panel.description) > 2000:
                raise ValueError("Invalid panel description")
            if len(panel.caption) > 220:
                raise ValueError(f"Page {page_index}, panel {panel_index}: caption has {len(panel.caption)} characters; maximum 220")
            lines = dialogue_lines(panel.dialogue)
            if len(lines) > 5:
                raise ValueError("A panel supports up to five speech balloons plus a caption")
            for speaker, line in lines:
                if not speaker.strip() or len(speaker) > 80 or not line.strip() or len(line) > 220:
                    raise ValueError("Each dialogue line needs a speaker and 1-220 characters of text")
            if panel.bubbleType not in {"SPEECH", "THOUGHT", "WHISPER", "SHOUT"}:
                raise ValueError("Unsupported bubble type")
            if panel.advanceTrigger not in {"TAP_TO_NEXT", "AUTO_TIMED", "MANUAL_SWIPE"}:
                raise ValueError("Unsupported advance trigger")
            if panel.durationMillis < 0 or (panel.advanceTrigger == "AUTO_TIMED" and not 500 <= panel.durationMillis <= 60000):
                raise ValueError("Invalid dialogue timing")



class StoryboardEdit(StrictModel):
    expectedRevision: str = Field(pattern="^[a-f0-9]{64}$")
    storyboard: Storyboard


class ArtworkApproval(StrictModel):
    revision: str = Field(pattern="^[a-f0-9]{64}$")
    approvedEstimateHigh: str = Field(pattern=r"^\d{1,5}\.\d{2}$")


def dialogue_lines(value: str):
    result = []
    for raw in value.splitlines():
        if not raw.strip():
            continue
        speaker, separator, line = raw.partition(":")
        if not separator:
            # Old saved jobs may wrap a single speaker's line across newlines.
            if not result:
                raise ValueError("Dialogue must name a speaker followed by a colon")
            previous_speaker, previous_line = result[-1]
            result[-1] = (previous_speaker, previous_line + " " + raw.strip())
            continue
        result.append((speaker.strip(), line.strip()))
    return result
