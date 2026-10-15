"""The one-time import of the service registry YAML into the people and
services tables (state/team.py, `helix team import`)."""

import textwrap

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from agent.ops.registry import REPO_ROOT, load_registry
from state import models
from state.models import Person, Service
from state.team import import_registry

REAL_YAML = REPO_ROOT / "config" / "service_registry.yaml"


def _counts(session: Session) -> tuple[int, int]:
    return (
        session.scalar(select(func.count()).select_from(Person)),
        session.scalar(select(func.count()).select_from(Service)),
    )


def test_real_registry_arrives_intact_with_everyone_owner(sqlite_engine):
    registry = load_registry(REAL_YAML)
    assert registry.services and registry.people
    with Session(sqlite_engine) as session:
        result = import_registry(session, registry)
        session.commit()
        assert result.imported_services == len(registry.services)
        assert result.imported_people == len(registry.people)
        assert result.skipped_reason is None
        for name, entry in registry.services.items():
            row = session.get(Service, name)
            assert row is not None
            assert row.team == entry.team
            assert row.repo == entry.repo
            assert row.default_owner == entry.default_owner
            assert row.owners == entry.owners
            assert row.default_reviewers == entry.default_reviewers
            assert row.depends_on == entry.depends_on
            assert row.namespace == entry.namespace
            assert row.path_owners == [
                {"pattern": r.pattern, "owners": r.owners} for r in entry.path_owners
            ]
        for login, person in registry.people.items():
            row = session.get(Person, login)
            assert row is not None
            assert row.name == person.name
            assert row.slack_id == person.slack
            assert row.role == "owner"
            assert row.email is None
            assert row.last_signed_in_at is None


def test_second_run_imports_nothing(sqlite_engine):
    registry = load_registry(REAL_YAML)
    with Session(sqlite_engine) as session:
        import_registry(session, registry)
        session.commit()
        before = _counts(session)
        again = import_registry(session, registry)
        session.commit()
        assert (again.imported_people, again.imported_services) == (0, 0)
        assert again.skipped_reason == (
            f"the team already has {before[0]} people and {before[1]} services"
        )
        assert _counts(session) == before


@pytest.mark.parametrize("existing", ["person", "service"])
def test_a_team_with_one_row_is_left_alone(sqlite_engine, existing):
    registry = load_registry(REAL_YAML)
    with Session(sqlite_engine) as session:
        if existing == "person":
            session.add(Person(login="solo", role="member"))
        else:
            session.add(Service(name="solo", owners=[], default_reviewers=[], depends_on=[]))
        session.commit()
        before = _counts(session)
        result = import_registry(session, registry)
        session.commit()
        assert result.skipped_reason is not None
        assert (result.imported_people, result.imported_services) == (0, 0)
        assert _counts(session) == before


def test_missing_file_imports_nothing(sqlite_engine, tmp_path):
    registry = load_registry(tmp_path / "nope.yaml")
    with Session(sqlite_engine) as session:
        result = import_registry(session, registry)
        assert (result.imported_people, result.imported_services) == (0, 0)
        assert _counts(session) == (0, 0)


def test_empty_yaml_imports_nothing(sqlite_engine, tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with Session(sqlite_engine) as session:
        result = import_registry(session, load_registry(path))
        assert (result.imported_people, result.imported_services) == (0, 0)
        assert _counts(session) == (0, 0)


def test_services_without_people(sqlite_engine, tmp_path):
    path = tmp_path / "svc.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            services:
              api:
                team: core
                owners: [zed]
                path_owners:
                  - {pattern: "*", owners: [zed]}
                  - {pattern: "db/*", owners: [amy]}
            """
        )
    )
    with Session(sqlite_engine) as session:
        result = import_registry(session, load_registry(path))
        assert (result.imported_people, result.imported_services) == (0, 1)
        row = session.get(Service, "api")
        assert row.path_owners[1] == {"pattern": "db/*", "owners": ["amy"]}
        assert row.repo == "" and row.default_owner is None and row.namespace is None


def test_cli_imports_the_real_file_not_the_benchmark_registry(tmp_path, monkeypatch, capsys):
    from helix import cli

    db = tmp_path / "team.db"
    engine = create_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", f"sqlite:///{db}")
    # tests/conftest.py points this at the benchmark cast (alice/bob/carol).
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", "benchmark/service_registry.yaml")
    assert cli.main(["team", "import"]) == 0
    out = capsys.readouterr().out
    assert "imported" in out
    with Session(engine) as session:
        assert session.get(Person, "alice") is None
        assert {p.login for p in session.scalars(select(Person))} == set(
            load_registry(REAL_YAML).people
        )
    assert cli.main(["team", "import"]) == 0
    assert "already has" in capsys.readouterr().out


def test_cli_missing_file_says_so(tmp_path, monkeypatch, capsys):
    from helix import cli

    db = tmp_path / "team.db"
    engine = create_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", f"sqlite:///{db}")
    assert cli.main(["team", "import", "--path", str(tmp_path / "nope.yaml")]) == 0
    assert "not found" in capsys.readouterr().out
    with Session(engine) as session:
        assert _counts(session) == (0, 0)
