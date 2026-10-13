"""Web front end for the card game solver.

Run with:  flask --app webapp run --debug

The battle model and search live in webapp.engine, which has no web dependencies and can
also run on its own:  python -m webapp.engine.solver
"""

import os


def create_app(test_config=None):
    """Application factory."""
    # Imported here so that running the engine alone does not load Flask or the web modules.
    # pylint: disable=import-outside-toplevel
    from flask import Flask, redirect, url_for

    from . import db, maps, roster

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE=os.path.join(app.instance_path, "wowscard.db"),
    )
    if test_config is None:
        app.config.from_pyfile("config.py", silent=True)
    else:
        app.config.from_mapping(test_config)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    app.register_blueprint(roster.bp)
    app.register_blueprint(maps.bp)

    @app.route("/")
    def index():
        return redirect(url_for("roster.index"))

    return app
