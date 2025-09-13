from pydantic import BaseModel


class AppCreate(BaseModel):
    name: str

    class Config:
        orm_mode = True
