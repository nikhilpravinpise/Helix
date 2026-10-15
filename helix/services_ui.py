"""Services page (future_plans.md "Team and services").

    GET  /ui/services                   list (any signed-in role)
    GET  /ui/services/new               the add form (admins; members are sent to the list)
    GET  /ui/services/{name}/edit       the edit form (admins; members are sent to the list)
    POST /ui/services                   add
    POST /ui/services/{name}            save an edit
    POST /ui/services/{name}/delete     remove

Everyone signed in can read the list; every POST needs an admin
(helix/auth.py `require_admin`). A form that breaks a rule
(state/services.py ServiceError) comes back with what was typed and the
message; a refused remove comes back as a notice in a short-lived cookie.

Field names keep order on submit: `reviewer` repeats in row order, and rule
row i posts `rule_pattern` (repeated, in row order) plus `rule_owners_<i>`.
"""

import re
from urllib.parse import parse_qs, quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from helix import auth
from helix.ui import DbSession, _incident_total, _pending_count, templates
from state import models, services
from state.services import ServiceError

FLASH_COOKIE = "helix_services_flash"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")

router = APIRouter(include_in_schema=False, dependencies=[auth.AdminOnly])

# notice code -> text; {name} is the service it is about.
_NOTICES = {
    "created": "Added {name}.",
    "updated": "Saved {name}.",
    "removed": "Removed {name}.",
}


def _back(code: str, name: str) -> RedirectResponse:
    return RedirectResponse(f"/ui/services?notice={code}&service={quote(name)}", status_code=303)


def _form_values(row: models.Service | None = None) -> dict:
    if row is None:
        return {
            "name": "",
            "team": "",
            "repo": "",
            "namespace": "",
            "depends_on": "",
            "owners": [],
            "default_owner": "",
            "reviewers": [],
            "rules": [],
        }
    return {
        "name": row.name,
        "team": row.team,
        "repo": row.repo,
        "namespace": row.namespace or "",
        "depends_on": "\n".join(row.depends_on or []),
        "owners": list(row.owners or []),
        "default_owner": row.default_owner or "",
        "reviewers": list(row.default_reviewers or []),
        "rules": [
            {"pattern": r.get("pattern", ""), "owners": list(r.get("owners") or [])}
            for r in row.path_owners or []
        ],
    }


async def _posted_form(request: Request) -> dict:
    body = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)

    def one(field: str) -> str:
        return (body.get(field) or [""])[0]

    return {
        "name": one("name"),
        "team": one("team"),
        "repo": one("repo"),
        "namespace": one("namespace"),
        "depends_on": one("depends_on"),
        "owners": body.get("owner", []),
        "default_owner": one("default_owner"),
        "reviewers": body.get("reviewer", []),
        "rules": [
            {"pattern": pattern, "owners": body.get(f"rule_owners_{i}", [])}
            for i, pattern in enumerate(body.get("rule_pattern", []))
        ],
    }


def _save_args(values: dict) -> dict:
    return {
        "team": values["team"],
        "repo": values["repo"],
        "namespace": values["namespace"],
        "owners": values["owners"],
        "default_owner": values["default_owner"],
        "default_reviewers": values["reviewers"],
        "depends_on": re.split(r"[,\n]", values["depends_on"]),
        "path_owners": values["rules"],
    }


def _form_page(
    request: Request, session, values: dict, *, editing: bool, error: str | None = None
) -> Response:
    people = list(session.scalars(select(models.Person).order_by(models.Person.created_at)))
    named = [p.login for p in people]
    # A login on the service who is no longer on the team stays visible, so
    # the save can say so and the admin can fix it.
    used = [
        *values["owners"],
        values["default_owner"],
        *values["reviewers"],
        *(o for rule in values["rules"] for o in rule["owners"]),
    ]
    extra = [login for login in dict.fromkeys(used) if login and login not in named]
    choices = [{"login": p.login, "name": p.name or p.login} for p in people] + [
        {"login": login, "name": login} for login in extra
    ]
    return templates.TemplateResponse(
        request,
        "services_form.html",
        {
            "active": "services",
            "editing": editing,
            "f": values,
            "choices": choices,
            "error": error,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
        400 if error else 200,
    )


@router.get("/ui/services", response_class=HTMLResponse)
def services_page(
    request: Request, session: DbSession, notice: str = "", service: str = ""
) -> Response:
    people = {p.login: p.name or p.login for p in session.scalars(select(models.Person))}
    rows = list(session.scalars(select(models.Service).order_by(models.Service.name)))
    flash = request.cookies.get(FLASH_COOKIE)
    if flash:
        found: tuple[str, str] | None = ("bad", unquote(flash))
    elif notice in _NOTICES:
        name = service if _NAME.fullmatch(service) else "the service"
        found = ("ok", _NOTICES[notice].format(name=name))
    else:
        found = None
    response = templates.TemplateResponse(
        request,
        "services.html",
        {
            "active": "services",
            "services": rows,
            "names": people,
            "can_manage": auth.may_manage(request, session),
            "notice": found,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
    )
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/ui/services")
    return response


@router.get("/ui/services/new", response_class=HTMLResponse)
def new_page(request: Request, session: DbSession) -> Response:
    if not auth.may_manage(request, session):
        return RedirectResponse("/ui/services", status_code=303)
    return _form_page(request, session, _form_values(), editing=False)


@router.get("/ui/services/{name}/edit", response_class=HTMLResponse)
def edit_page(request: Request, session: DbSession, name: str) -> Response:
    if not auth.may_manage(request, session):
        return RedirectResponse("/ui/services", status_code=303)
    row = session.get(models.Service, name)
    if row is None:
        raise HTTPException(404, f"There is no service called {name}.")
    return _form_page(request, session, _form_values(row), editing=True)


@router.post("/ui/services")
async def create(request: Request, session: DbSession) -> Response:
    values = await _posted_form(request)
    try:
        row = services.create_service(session, values["name"], **_save_args(values))
    except ServiceError as error:
        return _form_page(request, session, values, editing=False, error=str(error))
    return _back("created", row.name)


@router.post("/ui/services/{name}")
async def update(request: Request, session: DbSession, name: str) -> Response:
    if session.get(models.Service, name) is None:
        raise HTTPException(404, f"There is no service called {name}.")
    values = await _posted_form(request)
    try:
        services.update_service(session, name, values["name"], **_save_args(values))
    except ServiceError as error:
        values["name"] = name
        return _form_page(request, session, values, editing=True, error=str(error))
    return _back("updated", name)


@router.post("/ui/services/{name}/delete")
def delete(request: Request, session: DbSession, name: str) -> Response:
    try:
        services.delete_service(session, name)
    except ServiceError as error:
        response = RedirectResponse("/ui/services", status_code=303)
        response.set_cookie(
            FLASH_COOKIE,
            quote(str(error)),
            max_age=60,
            httponly=True,
            secure=auth._secure(request),
            samesite="lax",
            path="/ui/services",
        )
        return response
    return _back("removed", name)
