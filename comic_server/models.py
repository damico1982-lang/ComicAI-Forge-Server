from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttachmentReference(StrictModel):
    id: UUID
    useForAi: bool = False
    label: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)
    role: str = Field(default="DOCUMENT", pattern="^(CHARACTER|LOCATION|DOCUMENT)$")


class GenerationRequest(StrictModel):
    requestId: UUID
    rawScriptInput: str = Field(min_length=1, max_length=20000)
    selectedArtStyle: str = Field(min_length=1, max_length=100)
    targetPageCount: int = Field(ge=1, le=35, strict=True)
    panelsPerPage: int = Field(default=4, ge=3, le=6, strict=True)
    layoutStyle: str = Field(default="GRID", pattern="^(GRID|CINEMATIC)$")
    continuityNotes: str = Field(default="", max_length=6000)

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


def validate_storyboard(board: Storyboard, pages: int, panels_per_page: int = 4):
    if len(board.pages) != pages:
        raise ValueError("Planner returned the wrong page count")
    if not board.characterBible.strip() or len(board.characterBible) > 6000:
        raise ValueError("Invalid continuity guide")
    for page in board.pages:
        if len(page.panels) != panels_per_page:
            raise ValueError("Planner returned the wrong number of panels per page")
        for panel in page.panels:
            if not panel.description.strip() or len(panel.description) > 2000:
                raise ValueError("Invalid panel description")
            if len(panel.caption) > 220 or len(panel.dialogue) > 220:
                raise ValueError("Panel text is too long for lettering")
            if panel.dialogue.strip():
                speaker, separator, line = panel.dialogue.partition(":")
                if not separator or not speaker.strip() or len(speaker.strip()) > 80 or not line.strip():
                    raise ValueError("Dialogue must name a speaker and contain a line")
            if panel.bubbleType not in {"SPEECH", "THOUGHT", "WHISPER", "SHOUT"}:
                raise ValueError("Unsupported bubble type")
            if panel.advanceTrigger not in {"TAP_TO_NEXT", "AUTO_TIMED", "MANUAL_SWIPE"}:
                raise ValueError("Unsupported advance trigger")
            if panel.durationMillis < 0 or (panel.advanceTrigger == "AUTO_TIMED" and not 500 <= panel.durationMillis <= 60000):
                raise ValueError("Invalid dialogue timing")
