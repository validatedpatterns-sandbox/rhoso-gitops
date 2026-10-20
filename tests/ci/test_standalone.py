from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, TypedDict

import pytest
import yaml
from openshift.dynamic.exceptions import NotFoundError, ResourceNotFoundError

PATTERN_API_VERSION = "gitops.hybrid-cloud-patterns.io/v1alpha1"
APPLICATION_API_VERSION = "argoproj.io/v1alpha1"
OPERATOR_CORES_API_VERSION = "operators.coreos.com/v1alpha1"
OPENSTACK_API_VERSION = "operator.openstack.org/v1beta1"
CONTROLPLANE_API_VERSION = "core.openstack.org/v1beta1"
DATAPLANE_API_VERSION = "dataplane.openstack.org/v1beta1"

APPLICATION_ERROR_CONDITIONS = {
    "ComparisonError",
    "InvalidSpecError",
    "SharedResourceWarning",
    "SyncError",
}

REPOSITORY_ROOT = Path(__file__).parents[2]
CHART_PATH = REPOSITORY_ROOT / "charts" / "all" / "rhoso-gitops"
VALUE_FILES = (
    REPOSITORY_ROOT / "values-global.yaml",
    REPOSITORY_ROOT / "values-standalone.yaml",
    REPOSITORY_ROOT / "overrides" / "values-rhoso-gitops.yaml",
)
EXPECTED_RESOURCES_PATH = Path(__file__).parent / "expected-standalone.yaml"
POLL_INTERVAL = float(os.getenv("VP_RHOSO_POLL_INTERVAL", "5"))
TEST_TIMEOUT = float(os.getenv("VP_RHOSO_TEST_TIMEOUT", "300"))
START_TIME = time.monotonic()


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as yaml_file:
        return yaml.safe_load(yaml_file)


@pytest.fixture(scope="module")
def pattern_configuration() -> dict:
    global_values = load_yaml(VALUE_FILES[0])
    standalone_values = load_yaml(VALUE_FILES[1])
    pattern_name = global_values["global"]["pattern"]
    cluster_group = standalone_values["clusterGroup"]
    parent_application = cluster_group["applications"][pattern_name]
    return {
        "name": pattern_name,
        "namespace": "patterns-operator",
        "framework_application": f"{pattern_name}-{cluster_group['name']}",
        "parent_application": parent_application["name"],
    }


@pytest.fixture(scope="module")
def expected_resources() -> dict:
    return load_yaml(EXPECTED_RESOURCES_PATH)


@pytest.fixture(scope="module")
def rendered_applications() -> dict[str, dict]:
    command = ["helm", "template", "rhoso-gitops", str(CHART_PATH)]
    for value_file in VALUE_FILES:
        if not value_file.is_file():
            print(f"Skipping missing values file: {value_file}")
            continue
        command.extend(("--values", str(value_file)))

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        pytest.fail(f"helm is required to render RHOSO Applications: {exc}")

    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")

    applications: dict[str, dict] = {}
    for document in yaml.safe_load_all(result.stdout):
        if not isinstance(document, dict) or document.get("kind") != "Application":
            continue
        name = document.get("metadata", {}).get("name")
        if not name:
            pytest.fail("helm template rendered an Application without a name")
        if name in applications:
            pytest.fail(f"helm template rendered duplicate Application {name!r}")
        applications[name] = document

    if not applications:
        pytest.fail("helm template rendered no Applications")
    return applications


@pytest.fixture(scope="module")
def expected_application_names(
    pattern_configuration,
    rendered_applications,
) -> list[str]:
    return sorted(
        {
            pattern_configuration["framework_application"],
            pattern_configuration["parent_application"],
            *rendered_applications,
        }
    )


def discover_application_namespace(client, pattern_configuration) -> str:
    applications = client.resources.get(
        api_version=APPLICATION_API_VERSION,
        kind="Application",
    )
    pattern_name = pattern_configuration["name"]
    framework_applications = [
        application
        for application in applications.get().items or []
        if getattr(application.metadata, "labels", None)
        and application.metadata.labels.get("validatedpatterns.io/pattern")
        == pattern_name
    ]
    expected_names = {
        pattern_configuration["framework_application"],
        pattern_configuration["parent_application"],
    }
    actual_names = {application.metadata.name for application in framework_applications}
    assert actual_names == expected_names, (
        f"Expected framework Applications {sorted(expected_names)}, "
        f"got {sorted(actual_names)}"
    )

    namespaces = {
        application.metadata.namespace for application in framework_applications
    }
    assert (
        len(namespaces) == 1
    ), f"Framework Applications are spread across namespaces: {sorted(namespaces)}"
    return namespaces.pop()


def assert_field(actual: object, expected: object, context: str) -> None:
    assert actual == expected, f"{context}: expected {expected!r}, got {actual!r}"


def get_resource(client, api_version: str, kind: str, namespace: str, name: str):
    resource = client.resources.get(api_version=api_version, kind=kind)
    try:
        return resource.get(name=name, namespace=namespace)
    except NotFoundError as exc:
        raise AssertionError(f"{kind} {namespace}/{name} not found") from exc


def list_application_names(client, namespace: str) -> list[str]:
    applications = client.resources.get(
        api_version=APPLICATION_API_VERSION,
        kind="Application",
    )
    return sorted(
        application.metadata.name
        for application in applications.get(namespace=namespace).items or []
    )


class DeployedResource(TypedDict):
    name: str
    namespace: str | None


def application_resources(application, kind: str) -> list[DeployedResource]:
    return [
        {
            "name": resource_name(resource),
            "namespace": resource_namespace(resource),
        }
        for resource in getattr(application.status, "resources", None) or []
        if getattr(resource, "kind", None) == kind
    ]


def resource_name(resource) -> str:
    name = getattr(resource, "name", None)
    assert isinstance(name, str) and name, f"Resource has no name: {resource}"
    return name


def resource_namespace(resource) -> str | None:
    namespace = getattr(resource, "namespace", None)
    if namespace is None:
        return None
    assert isinstance(
        namespace, str
    ), f"Resource {resource_name(resource)} has an invalid namespace: {namespace}"
    return namespace


def child_application_names(client, namespace: str, parent_name: str) -> list[str]:
    parent = get_resource(
        client,
        APPLICATION_API_VERSION,
        "Application",
        namespace,
        parent_name,
    )
    children = application_resources(parent, "Application")
    assert children, f"Application {namespace}/{parent_name} has no child Applications"
    return sorted({child["name"] for child in children})


def condition_status(resource, condition_type: str) -> str | None:
    conditions = getattr(getattr(resource, "status", None), "conditions", None) or []
    for condition in conditions:
        if getattr(condition, "type", None) == condition_type:
            return getattr(condition, "status", None)
    return None


def wait_until(check: Callable[[], None], description: str) -> None:
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


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_pattern_reconciliation(openshift_dyn_client, pattern_configuration):
    pattern = pattern_configuration

    def check():
        resource = get_resource(
            openshift_dyn_client,
            PATTERN_API_VERSION,
            "Pattern",
            pattern["namespace"],
            pattern["name"],
        )
        last_step = getattr(getattr(resource, "status", None), "lastStep", None)
        assert_field(
            last_step,
            "reconcile complete",
            f"Pattern {pattern['namespace']}/{pattern['name']} status.lastStep",
        )

    wait_until(check, "Pattern reconciliation")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_application_inventory(
    openshift_dyn_client,
    pattern_configuration,
    expected_application_names,
    rendered_applications,
):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )
    expected = expected_application_names

    def check():
        parent_application = get_resource(
            openshift_dyn_client,
            APPLICATION_API_VERSION,
            "Application",
            namespace,
            pattern_configuration["parent_application"],
        )
        deployed_helm_applications = {
            resource.name
            for resource in getattr(parent_application.status, "resources", None) or []
            if getattr(resource, "kind", None) == "Application"
        }
        expected_helm_applications = set(rendered_applications)
        assert deployed_helm_applications == expected_helm_applications, (
            f"Deployed Helm Application resources in {namespace}: expected "
            f"{sorted(expected_helm_applications)}, got "
            f"{sorted(deployed_helm_applications)}"
        )

        applications = openshift_dyn_client.resources.get(
            api_version=APPLICATION_API_VERSION,
            kind="Application",
        )
        actual = sorted(
            application.metadata.name
            for application in applications.get(namespace=namespace).items or []
        )
        assert actual == expected, (
            f"Application inventory in {namespace}: expected {expected}, "
            f"got {actual}; missing {sorted(set(expected) - set(actual))}, "
            f"unexpected {sorted(set(actual) - set(expected))}"
        )

    wait_until(check, "Application inventory")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_application_convergence(
    openshift_dyn_client,
    pattern_configuration,
):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )

    def check():
        for application_name in list_application_names(
            openshift_dyn_client,
            namespace,
        ):
            application = get_resource(
                openshift_dyn_client,
                APPLICATION_API_VERSION,
                "Application",
                namespace,
                application_name,
            )
            context = f"Application {namespace}/{application_name}"
            status = getattr(application, "status", None)
            assert_field(
                getattr(getattr(status, "sync", None), "status", None),
                "Synced",
                f"{context} status.sync.status",
            )
            assert_field(
                getattr(getattr(status, "health", None), "status", None),
                "Healthy",
                f"{context} status.health.status",
            )
            conditions = getattr(status, "conditions", None) or []
            failed_conditions = [
                getattr(condition, "type", None)
                for condition in conditions
                if getattr(condition, "type", None) in APPLICATION_ERROR_CONDITIONS
            ]
            assert (
                not failed_conditions
            ), f"{context} has persistent error conditions: {failed_conditions}"

    wait_until(check, "Application convergence")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_application_sources(
    openshift_dyn_client,
    pattern_configuration,
    rendered_applications,
):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )
    sources = rendered_applications

    def check():
        for application_name in child_application_names(
            openshift_dyn_client,
            namespace,
            pattern_configuration["parent_application"],
        ):
            expected_document = sources.get(application_name)
            if expected_document is None:
                print(
                    "Skipping source assertion for deployed Application "
                    f"not present in the public render: {application_name}"
                )
                continue
            expected_source = expected_document["spec"]["source"]
            application = get_resource(
                openshift_dyn_client,
                APPLICATION_API_VERSION,
                "Application",
                namespace,
                application_name,
            )
            source = getattr(getattr(application, "spec", None), "source", None)
            context = f"Application {namespace}/{application_name} spec.source"
            assert source is not None, f"{context} is missing"
            for field in ("repoURL", "path", "targetRevision"):
                assert_field(
                    getattr(source, field, None),
                    expected_source[field],
                    f"{context}.{field}",
                )
            resolved_revision = getattr(
                getattr(getattr(application, "status", None), "sync", None),
                "revision",
                None,
            )
            assert (
                resolved_revision
            ), f"Application {namespace}/{application_name} has no resolved revision"

    wait_until(check, "Application sources")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_operator_subscription(
    openshift_dyn_client,
    pattern_configuration,
    expected_resources,
):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )
    expected_operator = expected_resources["operator"]

    def check():
        application = get_resource(
            openshift_dyn_client,
            APPLICATION_API_VERSION,
            "Application",
            namespace,
            "openstack-operator",
        )
        subscriptions = application_resources(application, "Subscription")
        assert (
            subscriptions
        ), "Application {}/openstack-operator has no deployed Subscriptions".format(
            namespace
        )
        assert len(subscriptions) == 1, (
            "Expected one OpenStack Subscription, got "
            f"{[subscription['name'] for subscription in subscriptions]}"
        )
        subscription_ref = subscriptions[0]
        operator = {
            "namespace": subscription_ref["namespace"] or "openstack-operators",
            "subscription": subscription_ref["name"],
            **expected_operator,
        }
        subscription = get_resource(
            openshift_dyn_client,
            OPERATOR_CORES_API_VERSION,
            "Subscription",
            operator["namespace"],
            operator["subscription"],
        )
        context = f"Subscription {operator['namespace']}/{operator['subscription']}"
        spec = getattr(subscription, "spec", None)
        subscription_status = getattr(subscription, "status", None)
        assert_field(
            getattr(spec, "channel", None),
            operator["channel"],
            f"{context} spec.channel",
        )
        assert_field(
            getattr(spec, "startingCSV", None),
            operator["startingCSV"],
            f"{context} spec.startingCSV",
        )
        assert_field(
            getattr(spec, "installPlanApproval", None),
            operator["installPlanApproval"],
            f"{context} spec.installPlanApproval",
        )
        assert_field(
            getattr(subscription_status, "installedCSV", None),
            operator["startingCSV"],
            f"{context} status.installedCSV",
        )
        assert getattr(subscription_status, "state", None) in {
            "AtLatestKnown",
            "UpgradePending",
        }, f"{context} status.state is not healthy"

        installed_csv = getattr(subscription_status, "installedCSV", None)
        assert installed_csv, f"{context} has no installed ClusterServiceVersion"
        csv = get_resource(
            openshift_dyn_client,
            OPERATOR_CORES_API_VERSION,
            "ClusterServiceVersion",
            operator["namespace"],
            installed_csv,
        )
        assert_field(
            getattr(getattr(csv, "status", None), "phase", None),
            "Succeeded",
            f"ClusterServiceVersion {operator['namespace']}/{installed_csv} phase",
        )

    wait_until(check, "Pinned operator installation")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_openstack_operator_ready(openshift_dyn_client, pattern_configuration):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )

    def check():
        application = get_resource(
            openshift_dyn_client,
            APPLICATION_API_VERSION,
            "Application",
            namespace,
            "openstack-operator-cr",
        )
        openstack_resources = application_resources(application, "OpenStack")
        assert (
            openstack_resources
        ), "Application {}/openstack-operator-cr has no deployed OpenStack CR".format(
            namespace
        )
        assert len(openstack_resources) == 1, (
            "Expected one OpenStack CR, got "
            f"{[resource['name'] for resource in openstack_resources]}"
        )
        openstack = openstack_resources[0]
        resource = get_resource(
            openshift_dyn_client,
            OPENSTACK_API_VERSION,
            "OpenStack",
            openstack["namespace"],
            openstack["name"],
        )
        status = condition_status(resource, "OpenStackOperatorReadyCondition")
        assert_field(
            status,
            "True",
            f"OpenStack {openstack['namespace']}/{openstack['name']} "
            "OpenStackOperatorReadyCondition",
        )

    wait_until(check, "OpenStack operator readiness")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_controlplane_ready(openshift_dyn_client, pattern_configuration):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )

    def check():
        application = get_resource(
            openshift_dyn_client,
            APPLICATION_API_VERSION,
            "Application",
            namespace,
            "openstack-controlplane",
        )
        controlplane_resources = application_resources(
            application,
            "OpenStackControlPlane",
        )
        assert controlplane_resources, (
            "Application {}/openstack-controlplane has no deployed "
            "OpenStackControlPlane resources".format(namespace)
        )
        for controlplane_resource in controlplane_resources:
            controlplane_namespace = controlplane_resource["namespace"] or "openstack"
            controlplane_name = controlplane_resource["name"]
            resource = get_resource(
                openshift_dyn_client,
                CONTROLPLANE_API_VERSION,
                "OpenStackControlPlane",
                controlplane_namespace,
                controlplane_name,
            )
            status = condition_status(resource, "Ready")
            assert_field(
                status,
                "True",
                f"OpenStackControlPlane {controlplane_namespace}/"
                f"{controlplane_name} Ready",
            )

    wait_until(check, "Control-plane readiness")


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_dataplane_ready(openshift_dyn_client, pattern_configuration):
    namespace = discover_application_namespace(
        openshift_dyn_client,
        pattern_configuration,
    )

    def check():
        application = get_resource(
            openshift_dyn_client,
            APPLICATION_API_VERSION,
            "Application",
            namespace,
            "openstack-dataplane",
        )
        node_sets = application_resources(
            application,
            "OpenStackDataPlaneNodeSet",
        )
        deployments = application_resources(
            application,
            "OpenStackDataPlaneDeployment",
        )
        assert node_sets, (
            "Application {}/openstack-dataplane has no deployed "
            "OpenStackDataPlaneNodeSet resources".format(namespace)
        )
        assert deployments, (
            "Application {}/openstack-dataplane has no deployed "
            "OpenStackDataPlaneDeployment resources".format(namespace)
        )
        for node_set in node_sets:
            dataplane_namespace = node_set["namespace"] or "openstack"
            node_set_name = node_set["name"]
            resource = get_resource(
                openshift_dyn_client,
                DATAPLANE_API_VERSION,
                "OpenStackDataPlaneNodeSet",
                dataplane_namespace,
                node_set_name,
            )
            context = f"OpenStackDataPlaneNodeSet {dataplane_namespace}/{node_set_name}"
            for condition_type in ("SetupReady", "Ready"):
                status = condition_status(resource, condition_type)
                assert_field(status, "True", f"{context} {condition_type}")

        for deployment in deployments:
            dataplane_namespace = deployment["namespace"] or "openstack"
            deployment_name = deployment["name"]
            resource = get_resource(
                openshift_dyn_client,
                DATAPLANE_API_VERSION,
                "OpenStackDataPlaneDeployment",
                dataplane_namespace,
                deployment_name,
            )
            context = (
                f"OpenStackDataPlaneDeployment {dataplane_namespace}/"
                f"{deployment_name}"
            )
            for condition_type in ("DeploymentReady", "Ready"):
                status = condition_status(resource, condition_type)
                assert_field(status, "True", f"{context} {condition_type}")

    wait_until(check, "Data-plane readiness")
