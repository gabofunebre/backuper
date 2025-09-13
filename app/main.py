from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from . import models, schemas
from .database import SessionLocal, engine

models.Base.metadata.create_all(bind=engine)

app = FastAPI()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/apps", response_model=schemas.AppCreate)
def create_app(app_in: schemas.AppCreate, db: Session = Depends(get_db)):
    existing = db.query(models.App).filter(models.App.name == app_in.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="App already exists")
    db_app = models.App(name=app_in.name)
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app


@app.get("/apps", response_model=list[schemas.AppCreate])
def list_apps(db: Session = Depends(get_db)):
    apps = db.query(models.App).all()
    return apps


@app.delete("/apps/{app_id}")
def delete_app(app_id: int, db: Session = Depends(get_db)):
    app_db = db.query(models.App).get(app_id)
    if not app_db:
        raise HTTPException(status_code=404, detail="App not found")
    db.delete(app_db)
    db.commit()
    return {"detail": "App deleted"}
