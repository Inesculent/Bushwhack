from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn


app = FastAPI(title="Orchestrator API")

class ReviewPayload(BaseModel):
    repository_path: str
    branch: str
    diff: str


@app.post("/review")
async def receive_review_request(payload: ReviewPayload):
    """
    Catches the git diff from the CLI tool and begins the orchestration process.
    """
    print(f"Received review request for repository: {payload.repository_path}, branch: {payload.branch}")

    # TODO: Implement the orchestration process

    return {"status": "approved"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)