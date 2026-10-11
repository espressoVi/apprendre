from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from routers import api

app = FastAPI(title="Apprendre")
templates = Jinja2Templates(directory="templates")

app.include_router(api.router)

@app.get("/", response_class=HTMLResponse)
async def serve_spa(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
