# Tavon Partners — Statement Checker Backend

FastAPI service that parses merchant card-processing statements
(AIB, Clover, Elavon, Global Payments, InterCard, Dojo, Trust Payments)
and returns the true blended rate. Nothing uploaded is ever stored -
each PDF is processed in a temp file that's deleted immediately after
the request, whether it succeeds or fails.

## Deploying on Render

1. Push this folder to a new GitHub repo.
2. On Render: New -> Web Service -> connect that repo.
3. Render auto-detects the Dockerfile - no build/start command needed.
4. Before going live, edit `app/main.py` and change:
   `allow_origins=["*"]` to `allow_origins=["https://<your-real-domain>"]`
5. Once deployed, Render gives you a URL like
   `https://tavon-statement-backend.onrender.com` - point the site's
   upload form at `<that-url>/analyze`.

## Local testing

    pip install -r requirements.txt
    uvicorn app.main:app --reload
    curl -X POST http://localhost:8000/analyze -F "file=@statement.pdf"
