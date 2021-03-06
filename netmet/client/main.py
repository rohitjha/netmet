# Copyright 2017: GoDaddy Inc.

import json
import logging
import threading

import flask
from flask_helpers import routing
import jsonschema

from netmet.client import collector
from netmet.client import conf
from netmet import config
from netmet.utils import secure
from netmet.utils import status


APP = flask.Flask(__name__, static_folder=None)
APP.wsgi_app = status.StatusMiddleware(APP)
LOG = logging.getLogger(__name__)

# TOOD(boris-42): Move this to the Collector (unify with server).
_LOCK = threading.Lock()
_COLLECTOR = None
_CONFIG = None
_DEAD = False


def _destroy_collector():
    global _LOCK, _COLLECTOR, _CONFIG

    locked = False
    try:
        locked = _LOCK.acquire(False)
        if locked:
            if _COLLECTOR:
                _COLLECTOR.stop()
                _COLLECTOR = None
                _CONFIG = None
    finally:
        if locked:
            _LOCK.release()


@APP.errorhandler(404)
def not_found(error):
    """404 Page in case of failures."""
    return flask.jsonify({"error": "Not Found"}), 404


@APP.errorhandler(500)
def internal_server_error(error):
    """500 Handle Internal Errors."""
    return flask.jsonify({"error": "Internal Server Error"}), 500


@APP.route("/api/v2/config", methods=['GET'])
@APP.route("/api/v1/config", methods=['GET'])
def get_config():
    """Returns netmet config."""
    global _CONFIG

    if _CONFIG:
        return flask.jsonify({"config": _CONFIG}), 200
    else:
        return flask.jsonify({"error": "Netmet is not configured"}), 404


@APP.route("/api/v2/config", methods=['POST'])
@secure.check_hmac_auth
def set_config_v2():
    global _LOCK, _COLLECTOR, _CONFIG

    if _DEAD:
        flask.abort(500)

    schema = {
        "type": "object",
        "definitions": {
            "client": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "ip": {"type": "string"},
                    "port": {"type": "integer"},
                    "hypervisor": {"type": "string"},
                    "az": {"type": "string"},
                    "dc": {"type": "string"}
                },
                "required": ["ip", "host", "az", "dc", "port"],
                "additionProperties": False
            },
            "settings": {
                "type": "object",
                "properties": {
                    "packet_size": {"type": "number", "minimum": 1},
                    "period": {"type": "number", "minimum": 0.1},
                    "timeout": {"type": "number", "minimum": 0.01}
                },
                "required": ["period", "timeout"],
                "additionProperties": False
            }
        },
        "properties": {
            "netmet_server": {"type": "string"},
            "client_host": {"$ref": "#/definitions/client"},
            "settings": {"$ref": "#/definitions/settings"},
            "tasks": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "north-south": {
                                    "type": "object",
                                    "properties": {
                                        "dest": {"type": "string"},
                                        "protocol": {"enum": ["http", "icmp"]},
                                        "settings": {
                                            "$ref": "#/definitions/settings"
                                        }
                                    },
                                    "required": ["dest", "protocol"],
                                    "additionalProperties": False
                                }
                            },
                            "required": ["north-south"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "east-west": {
                                    "type": "object",
                                    "properties": {
                                        "dest": {
                                            "$ref": "#/definitions/client"
                                        },
                                        "protocol": {"enum": ["http", "icmp"]},
                                        "settings": {
                                            "$ref": "#/definitions/settings"
                                        }
                                    },
                                    "required": ["dest", "protocol"],
                                    "additionalProperties": False
                                }
                            },
                            "required": ["east-west"],
                            "additionalProperties": False
                        }
                    ]
                }
            }
        },
        "required": ["netmet_server", "client_host", "tasks", "settings"],
        "additionalProperties": False
    }

    try:
        data = flask.request.get_json(silent=False, force=True)
        jsonschema.validate(data, schema)
        settings = data.pop("settings")
        settings.setdefault("packet_size", 55)
        for task in data["tasks"]:
            task[task.keys()[0]].setdefault("settings", {})
            for k, v in settings.iteritems():
                task[task.keys()[0]]["settings"].setdefault(k, v)

        LOG.info("Applying new config")
        LOG.info(json.dumps(data, indent=2))
    except (ValueError, jsonschema.exceptions.ValidationError) as e:
        return flask.jsonify({"error": "Bad request: %s" % e}), 400

    with _LOCK:
        if _COLLECTOR:
            _COLLECTOR.stop()

        _CONFIG = data
        conf.restore_url_set(data["netmet_server"],
                             data["client_host"]["host"],
                             data["client_host"]["port"])
        _COLLECTOR = collector.Collector(**data)
        _COLLECTOR.start()

    return flask.jsonify({"message": "Succesfully update netmet config"}), 201


@APP.route("/api/v1/unregister", methods=['POST'])
@APP.route("/api/v2/unregister", methods=['POST'])
@secure.check_hmac_auth
def unregister():
    """Stops collector system."""
    conf.restore_url_clear(config.get("port"))
    _destroy_collector()
    return flask.jsonify({"message": "Netmet clinet is unregistered."}), 201


APP = routing.add_routing_map(APP, html_uri=None, json_uri="/")


def die():
    global _DEAD
    _DEAD = True
    _destroy_collector()


def load():
    conf.restore.async_call(config.get("hmac_keys"), config.get("port"))
    return APP
