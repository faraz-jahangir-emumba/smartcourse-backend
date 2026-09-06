"""The SmartCourse API.

This is the entry point. Uvicorn imports the `app` object below and serves it.
"""

from fastapi import FastAPI

# Creating the application. The title and version show up on the /docs page
# that FastAPI generates for you.
app = FastAPI(
    title="SmartCourse API",
    description="Backend for EduCorp's course delivery platform.",
    version="0.1.0",
)


# @app.get("/health") tells FastAPI: when a GET request arrives for /health,
# run this function. The dictionary returned is converted to JSON automatically.
#
# `-> dict[str, str]` is a type hint. Python does not enforce it at runtime,
# but FastAPI reads it to build the documentation, and the type checker we add
# later uses it to catch mistakes before the code runs.
@app.get("/health")
async def health() -> dict[str, str]:
    """Says the service is up. Nothing more.

    Later this gets a sibling that also checks the database and Redis are
    reachable - a service can be running while being unable to do any work.
    """
    return {"status": "ok"}
