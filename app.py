from flask import Flask

app = Flask(__name__)


@app.get("/")
def hello():
    """Also atlasflow's health check path — must stay a fast, unauthenticated
    2xx with no redirect. See README → "Deploying to atlasflow"."""
    return "Hello, World!"
