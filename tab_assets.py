#!/usr/bin/env python3
"""Shared static-file serving helper for tab-specific CSS/JS assets.

Each tab's routes.py calls register() once to expose its own tab.css /
<tab>-tab.js files as GET routes with correct Content-Type headers, instead
of hand-writing duplicate static-file handlers per tab.
"""
import os


def _make_static_handler(file_path, content_type):
    def _handler(h, params):
        if not os.path.isfile(file_path):
            h.send_error(404, "Asset not found")
            return
        with open(file_path, "rb") as fh:
            body = fh.read()
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        h.wfile.write(body)
    return _handler


def register(get_routes, tab_dir, css_route=None, css_file="tab.css",
             js_route=None, js_file=None):
    """Register GET handlers serving tab_dir/css_file and tab_dir/js_file.

    css_route / js_route: URL paths to register (e.g. "/mr-tab.css").
    Pass None to skip registering that asset. js_file must be given
    explicitly since filenames differ per tab (e.g. "meg-tab.js",
    "mr-tab.js", "pet-tab.js").
    """
    if css_route:
        css_path = os.path.join(tab_dir, css_file)
        get_routes[css_route] = _make_static_handler(css_path, "text/css; charset=utf-8")
    if js_route:
        if not js_file:
            raise ValueError("js_file is required when js_route is set")
        js_path = os.path.join(tab_dir, js_file)
        get_routes[js_route] = _make_static_handler(js_path, "application/javascript; charset=utf-8")
