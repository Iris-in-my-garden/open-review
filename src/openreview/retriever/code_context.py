from pydantic import BaseModel


class CodeContext(BaseModel):
    file_path: str
    reason: str
    snippet: str
    score: float
    revision: str = "head"