"""GitOps setup and assertions for the k-orc (ORC) integration.

Unlike the rest of the pattern CI (which is purely read-only against an
already-deployed pattern), this module *creates* the k-orc Argo CD
Applications, from the manifests in tests/ci/k-orc/applications/:

- `k-orc-crds` and `k-orc-operator` are static (OCI Helm chart sources)
  and are applied as-is;
- `k-orc-resources` is generated: its git source (the pattern's own
  repository, i.e. the repository these tests run from) is filled in at
  runtime.

Everything else is deployed by those Applications through GitOps: the ORC
CRDs and controller from the official Helm charts, then the credentials
Job and the ORC smoke stack (Flavor, Image, Network, Subnet, Port, Server)
from tests/ci/k-orc/resources/.

The Applications and their resources are steady-state: they are left in
place after the run, and subsequent runs re-apply them idempotently.
Applications carrying the validatedpatterns.io/ci-feature label are
excluded from the pattern inventory assertions in test_standalone.py.

The tests are collected through tests/ci/test_standalone.py, which
star-imports this module (the "k-orc" directory name is not importable as a
Python package; tests/ci/conftest.py puts it on sys.path).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from openshift.dynamic.exceptions import NotFoundError, ResourceNotFoundError

ORC_API_VERSION = "openstack.k-orc.cloud/v1alpha1"
ARGO_APPLICATION_API_VERSION = "argoproj.io/v1alpha1"

# Namespaces and object names, mirroring the manifests under tests/ci/.
ORC_CONTROLLER_NAMESPACE = "orc-system"
ORC_RESOURCE_NAMESPACE = "openstack"
ORC_RESOURCES_APP_NAME = "k-orc-resources"

ORC_EXPECTED_CRDS = (
    "flavors.openstack.k-orc.cloud",
    "images.openstack.k-orc.cloud",
    "networks.openstack.k-orc.cloud",
    "subnets.openstack.k-orc.cloud",
    "ports.openstack.k-orc.cloud",
    "servers.openstack.k-orc.cloud",
)

# (kind, object name) pairs mirroring tests/ci/k-orc/deploy/resources/.
ORC_EXPECTED_RESOURCES = (
    ("Flavor", "k-orc-flavor"),
    ("Image", "cirros"),
    ("Network", "k-orc-network"),
    ("Subnet", "k-orc-subnet"),
    ("Port", "k-orc-port"),
    ("Server", "k-orc-server"),
)

APPLICATION_ERROR_CONDITIONS = {
    "ComparisonError",
    "InvalidSpecError",
    "SharedResourceWarning",
    "SyncError",
}

ORC_TERMINAL_REASONS = {"InvalidConfiguration", "UnrecoverableError"}

PATTERN_LABEL = "validatedpatterns.io/pattern"
CI_FEATURE_LABEL = "validatedpatterns.io/ci-feature"

K_ORC_DIR = Path(__file__).parent
APPLICATIONS_DIR = K_ORC_DIR / "applications"
REPOSITORY_ROOT = K_ORC_DIR.parents[2]

POLL_INTERVAL = float(os.getenv("VP_KORC_POLL_INTERVAL", "5"))
TEST_TIMEOUT = float(os.getenv("VP_KORC_TEST_TIMEOUT", "900"))
START_TIME = time.monotonic()


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as yaml_file:
        return yaml.safe_load(yaml_file)


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def pattern_name() -> str:
    values_global = load_yaml(REPOSITORY_ROOT / "values-global.yaml")
    return values_global["global"]["pattern"]


def k_orc_application_names() -> list:
    """Application names from tests/ci/k-orc/applications/*.yaml."""
    names = []
    for path in sorted(APPLICATIONS_DIR.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(document, dict) and document.get("kind") == "Application":
                names.append(document["metadata"]["name"])
    assert names, f"No Applications found in {APPLICATIONS_DIR}"
    return sorted(set(names))


K_ORC_APPLICATIONS = tuple(k_orc_application_names())


def wait_until(check: Callable[[], None], description: str) -> None:
    """Poll check() until it stops raising or TEST_TIMEOUT expires."""
    last_error = "condition did not run"
    while True:
        try:
            check()
        except (
            AssertionError,
            AttributeError,
            NotFoundError,
            ResourceNotFoundError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            return

        elapsed = time.monotonic() - START_TIME
        remaining = TEST_TIMEOUT - elapsed
        if remaining <= 0:
            pytest.fail(
                f"{description} not satisfied within {TEST_TIMEOUT:g}s: {last_error}"
            )
        time.sleep(min(POLL_INTERVAL, remaining))


def get_resource(client, api_version: str, kind: str, namespace: str, name: str):
    resource = client.resources.get(api_version=api_version, kind=kind)
    try:
        return resource.get(name=name, namespace=namespace)
    except NotFoundError as exc:
        raise AssertionError(f"{kind} {namespace}/{name} not found") from exc


def argo_namespace(client) -> str:
    """Discover the namespace of the Argo CD instance managing the pattern."""
    applications = client.resources.get(
        api_version=ARGO_APPLICATION_API_VERSION,
        kind="Application",
    )
    namespaces = {
        application.metadata.namespace
        for application in applications.get().items or []
        if getattr(application.metadata, "labels", None)
        and application.metadata.labels.get(PATTERN_LABEL) == pattern_name()
    }
    assert namespaces, "No pattern Applications found on the cluster"
    assert (
        len(namespaces) == 1
    ), f"Pattern Applications are spread across namespaces: {sorted(namespaces)}"
    return namespaces.pop()


def parent_application_source(client, namespace: str) -> tuple[str, str]:
    """Return (repoURL, targetRevision) of the deployed pattern repository.

    The parent Application (the pattern's main application, rendered by the
    pattern chart) tracks the repository and branch the pattern was deployed
    from.
    """
    values_global = load_yaml(REPOSITORY_ROOT / "values-global.yaml")
    values_standalone = load_yaml(REPOSITORY_ROOT / "values-standalone.yaml")
    cluster_group = values_standalone["clusterGroup"]
    parent_name = cluster_group["applications"][values_global["global"]["pattern"]][
        "name"
    ]
    parent = get_resource(
        client,
        ARGO_APPLICATION_API_VERSION,
        "Application",
        namespace,
        parent_name,
    )
    spec = getattr(parent, "spec", None)
    source = getattr(spec, "source", None)
    assert (
        source is not None
    ), f"Application {namespace}/{parent_name} has no spec.source"
    return source.repoURL, source.targetRevision


def resolve_application_source(client, namespace: str) -> tuple[str, str]:
    """Resolve the source for the k-orc Application.

    Precedence: explicit environment overrides, then the local git checkout
    (origin remote and current branch: these tests run from the repository
    whose k-orc manifests should be deployed), then the deployed parent
    Application's source.
    """
    repo_url = os.getenv("VP_KORC_REPOURL")
    target_revision = os.getenv("VP_KORC_TARGET_REVISION")
    if not repo_url:
        origin = git_output("remote", "get-url", "origin")
        if origin:
            repo_url = origin
    if not target_revision:
        branch = git_output("branch", "--show-current")
        if branch:
            target_revision = branch
    if not repo_url or not target_revision:
        parent_url, parent_revision = parent_application_source(client, namespace)
        repo_url = repo_url or parent_url
        target_revision = target_revision or parent_revision
    assert repo_url and target_revision, (
        "Could not resolve the pattern repository source for the k-orc "
        "Application (set VP_KORC_REPOURL / VP_KORC_TARGET_REVISION)"
    )
    return repo_url, target_revision


_APPLIED: dict[str, str | None] = {"namespace": None}


def apply_k_orc_applications(client, namespace: str) -> None:
    """Create or update the k-orc Applications from applications/.

    Static manifests (OCI chart sources) are applied as-is; generated ones
    (empty repoURL/targetRevision, i.e. k-orc-resources) get the pattern
    repository source filled in. All are placed in the Argo CD namespace
    discovered on the cluster.
    """
    repo_url, target_revision = resolve_application_source(client, namespace)

    applications = client.resources.get(
        api_version=ARGO_APPLICATION_API_VERSION,
        kind="Application",
    )
    for path in sorted(APPLICATIONS_DIR.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict) or document.get("kind") != "Application":
                continue
            name = document["metadata"]["name"]
            document["metadata"]["namespace"] = namespace
            source = document["spec"]["source"]
            if not source.get("repoURL"):
                source["repoURL"] = repo_url
            if not source.get("targetRevision"):
                source["targetRevision"] = target_revision
            try:
                applications.get(name=name, namespace=namespace)
            except NotFoundError:
                applications.create(body=document)
            else:
                # Converge the spec to the manifest (idempotent merge patch;
                # CRDs do not accept the client's default strategic merge
                # patch).
                applications.patch(
                    body={"spec": document["spec"]},
                    name=name,
                    namespace=namespace,
                    content_type="application/merge-patch+json",
                )


@pytest.fixture()
def k_orc_applications(openshift_dyn_client) -> tuple:
    """Apply the k-orc Applications; return (client, namespace).

    The Applications are intentionally left in place after the tests: the
    smoke stack is steady-state and re-applying is idempotent.

    The openshift_dyn_client fixture is function-scoped (vp-qe-test-common
    v1), so this fixture is function-scoped too and memoizes its work: the
    Applications are verified (and re-created if missing) rather than fully
    re-resolved on every test.
    """
    client = openshift_dyn_client
    applications = client.resources.get(
        api_version=ARGO_APPLICATION_API_VERSION,
        kind="Application",
    )

    if _APPLIED["namespace"]:
        namespace = _APPLIED["namespace"]
        missing = False
        for name in K_ORC_APPLICATIONS:
            try:
                applications.get(name=name, namespace=namespace)
            except NotFoundError:
                missing = True
                break
        if not missing:
            return client, namespace

    namespace = argo_namespace(client)
    apply_k_orc_applications(client, namespace)
    _APPLIED["namespace"] = namespace
    return client, namespace


def condition_status(resource, condition_type: str) -> tuple[str | None, str, str]:
    """Return (status, reason, message) for a condition type."""
    conditions = getattr(getattr(resource, "status", None), "conditions", None) or []
    for condition in conditions:
        if getattr(condition, "type", None) == condition_type:
            return (
                getattr(condition, "status", None),
                getattr(condition, "reason", None) or "",
                getattr(condition, "message", None) or "",
            )
    return None, "", ""


def assert_orc_object_conditions(kind: str, name: str, resource) -> None:
    """Assert an ORC object is Available and not Progressing.

    Terminal error reasons fail immediately: they never resolve without spec
    changes, so polling would only waste the timeout budget.
    """
    context = f"{kind} {ORC_RESOURCE_NAMESPACE}/{name}"
    progressing, reason, message = condition_status(resource, "Progressing")
    if progressing == "True" and reason in ORC_TERMINAL_REASONS:
        pytest.fail(f"{context} is in terminal state: {reason}: {message}")
    available, _, available_message = condition_status(resource, "Available")
    assert (
        progressing == "False"
    ), f"{context} Progressing is {progressing!r} (reason {reason!r}): {message}"
    assert (
        available == "True"
    ), f"{context} Available is {available!r}: {available_message}"
    resource_id = getattr(getattr(resource, "status", None), "id", None)
    assert resource_id, f"{context} has no status.id"


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_k_orc_applications(openshift_dyn_client, k_orc_applications):
    _, namespace = k_orc_applications

    def check():
        for application_name in K_ORC_APPLICATIONS:
            application = get_resource(
                openshift_dyn_client,
                ARGO_APPLICATION_API_VERSION,
                "Application",
                namespace,
                application_name,
            )
            context = f"Application {namespace}/{application_name}"
            status = getattr(application, "status", None)
            assert (
                getattr(getattr(status, "sync", None), "status", None) == "Synced"
            ), f"{context} is not Synced"
            assert (
                getattr(getattr(status, "health", None), "status", None) == "Healthy"
            ), f"{context} is not Healthy"
            failed_conditions = [
                getattr(condition, "type", None)
                for condition in getattr(status, "conditions", None) or []
                if getattr(condition, "type", None) in APPLICATION_ERROR_CONDITIONS
            ]
            assert (
                not failed_conditions
            ), f"{context} has persistent error conditions: {failed_conditions}"

    wait_until(check, "k-orc Argo CD applications convergence")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_orc_crds(openshift_dyn_client, k_orc_applications):
    def check():
        crds = openshift_dyn_client.resources.get(
            api_version="apiextensions.k8s.io/v1", kind="CustomResourceDefinition"
        )
        existing = {item.metadata.name for item in crds.get().items or []}
        missing = sorted(set(ORC_EXPECTED_CRDS) - existing)
        assert not missing, f"Missing ORC CRDs: {missing}"

    wait_until(check, "ORC CRDs installed")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_orc_controller_available(openshift_dyn_client, k_orc_applications):
    def check():
        deployments = openshift_dyn_client.resources.get(
            api_version="apps/v1", kind="Deployment"
        )
        orc_deployments = [
            deployment
            for deployment in deployments.get(namespace=ORC_CONTROLLER_NAMESPACE).items
            or []
            if (getattr(deployment.metadata, "labels", None) or {}).get(
                "app.kubernetes.io/name"
            )
            == "orc"
        ]
        assert (
            orc_deployments
        ), f"No ORC controller deployment found in {ORC_CONTROLLER_NAMESPACE}"
        for deployment in orc_deployments:
            context = (
                f"Deployment {ORC_CONTROLLER_NAMESPACE}/{deployment.metadata.name}"
            )
            conditions = getattr(deployment.status, "conditions", None) or []
            available = [
                getattr(condition, "status", None)
                for condition in conditions
                if getattr(condition, "type", None) == "Available"
            ]
            assert available == ["True"], f"{context} is not Available"

    wait_until(check, "ORC controller availability")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
@pytest.mark.parametrize("kind,name", ORC_EXPECTED_RESOURCES)
def test_orc_resource(openshift_dyn_client, k_orc_applications, kind: str, name: str):
    def check():
        resource = get_resource(
            openshift_dyn_client,
            ORC_API_VERSION,
            kind,
            ORC_RESOURCE_NAMESPACE,
            name,
        )
        assert_orc_object_conditions(kind, name, resource)

    wait_until(check, f"ORC {kind} {name} readiness")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_orc_server_uses_managed_resources(openshift_dyn_client, k_orc_applications):
    """The server must boot from the managed image on the managed network."""

    def check():
        server = get_resource(
            openshift_dyn_client,
            ORC_API_VERSION,
            "Server",
            ORC_RESOURCE_NAMESPACE,
            "k-orc-server",
        )
        image = get_resource(
            openshift_dyn_client,
            ORC_API_VERSION,
            "Image",
            ORC_RESOURCE_NAMESPACE,
            "cirros",
        )
        port = get_resource(
            openshift_dyn_client,
            ORC_API_VERSION,
            "Port",
            ORC_RESOURCE_NAMESPACE,
            "k-orc-port",
        )

        server_status = getattr(server, "status", None)
        image_id = getattr(getattr(image, "status", None), "id", None)
        assert image_id, "Image cirros has no status.id"
        server_image_id = getattr(
            getattr(server_status, "resource", None), "imageID", None
        )
        assert server_image_id == image_id, (
            f"Server k-orc-server boots image {server_image_id!r}, "
            f"expected the managed image {image_id!r}"
        )

        port_resource = getattr(getattr(port, "status", None), "resource", None)
        fixed_ips = getattr(port_resource, "fixedIPs", None) or []
        assert fixed_ips, "Port k-orc-port has no fixed IP allocated"

    wait_until(check, "ORC server to managed resources attachment")
