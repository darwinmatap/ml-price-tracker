from fastapi import FastAPI

app = FastAPI(title="ML Price Tracker")


@app.get("/health")
def health_check():
    return {"status": "ok"}
