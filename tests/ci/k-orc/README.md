# k-orc: OpenStack Resource Controller integration

This directory deploys [ORC][orc] (OpenStack Resource Controller) on the
cluster and manages a small steady-state smoke stack in OpenStack through it:

- a **Flavor** (`m1.xsmall`: 1024 MiB RAM, 2 vCPUs, 15 GiB disk),
- a CirrOS **Image** (downloaded by ORC from download.cirros-cloud.net,
  pinned by sha256),
- a **Network**, **Subnet**, and **Port**,
- a **Server** (VM) booting the managed image with the managed flavor on the
  managed port.

The goal is twofold: exercise ORC end-to-end against a real RHOSO cloud, and
give the pattern CI meaningful assertions (see `test_k_orc.py`).

This is a CI-only feature: nothing outside `tests/ci/` is modified, and the
standard pattern chart does not deploy it.

[orc]: https://github.com/k-orc/openstack-resource-controller

## Layout

```text
k-orc/
  applications/             # Argo CD Applications, applied by the CI tests
    k-orc-crds.yaml         #   static: orc-crds chart (CRDs), wave -2
    k-orc-operator.yaml     #   static: orc chart (controller), wave -1
    k-orc-resources.yaml    #   template: git source filled in by the tests
  resources/                # Source path of k-orc-resources (flat directory)
    00-credentials-rbac.yaml  # SA/Role/RoleBinding for the credentials Job
    01-credentials-job.yaml   # assembles the ORC credential Secret
    10-flavor.yaml ... 15-server.yaml  # ORC custom resources (wave 0)
  test_k_orc.py             # CI assertions (applies the Applications, then polls)
```

## How it is deployed

The CI tests (not the pattern chart) create the Argo CD Applications, in the
namespace of the Argo CD instance managing the pattern:

1. `test_k_orc.py` reads every manifest in `applications/` and applies it
   (create or idempotent patch). Static manifests (`k-orc-crds`,
   `k-orc-operator`: OCI Helm chart sources) are applied as-is. The
   generated one (`k-orc-resources`) gets its Git source filled in:
   `VP_KORC_REPOURL` / `VP_KORC_TARGET_REVISION` environment variables,
   then the local Git checkout (origin remote and current branch), then the
   deployed parent Application's source.
2. `k-orc-crds` and `k-orc-operator` sync the official ORC Helm charts from
   quay.io into `orc-system` (CRDs first, then the controller).
3. `k-orc-resources` syncs `resources/` into the `openstack` namespace.
   Until the ORC CRDs are established, Argo CD rejects its sync plan
   ("one or more synchronization tasks are not valid"); the Application's
   sync retries (limit 20, exponential backoff) make it converge once the
   CRDs are in place. Within that Application, sync waves order the
   credentials RBAC (-2), the credentials Job (-1) and the ORC custom
   resources (0).

The Applications and everything they deploy are steady-state: they are left
running after the CI run, and re-applying is idempotent. To remove the whole
feature, delete the three Applications (their finalizer cascade removes the
CRDs, the controller, the ORC resources and, through ORC, the OpenStack
resources themselves).

Applications carrying the `validatedpatterns.io/ci-feature: k-orc` label are
excluded from the pattern inventory assertions in `test_standalone.py`,
since they are created by the CI rather than by the pattern chart.

## Version pinning

Both Helm charts (`orc-crds` and `orc`) are pinned to the same version in
`applications/`. Bump both together. `3.0.0-pre1` was the only published
chart tag at the time this was written; prefer a stable release once one is
available.

## OpenStack credentials

ORC resources reference `spec.cloudCredentialsRef` pointing at the
`orc-cloud-config` Secret in the `openstack` namespace. That Secret is
assembled by the `k-orc-credentials` Job from RHOSO's own configuration:

- `openstack-config` ConfigMap: `clouds.yaml` (cloud `default`, without the
  password),
- `openstack-config-secret` Secret: `cloudrc` (carries `OS_PASSWORD`),
- `combined-ca-bundle` Secret: the TLS CA bundle (exposed to ORC as the
  optional `cacert` key).

The password is only ever handled in memory inside the Job; it is never
printed, logged, or committed. If the RHOSO admin password is rotated,
delete the completed `k-orc-credentials` Job object: the next Argo sync
re-runs it and refreshes the Secret. The same applies after editing the Job
manifest itself (Jobs are immutable).

The credential account is the RHOSO admin (`clouds.yaml` entry `default`).
Creating flavors requires elevated OpenStack permissions; if the deployment
uses a restricted account, the `k-orc-flavor` object will report a terminal
`InvalidConfiguration`/`UnrecoverableError` condition.

## Configuration knobs

- **Subnet CIDR**: `192.168.150.0/24` by default, in
  `resources/13-subnet.yaml`. Change it there if it overlaps existing
  networks. `cidr` is immutable on the Subnet object: after changing it,
  delete the `k-orc-subnet`, `k-orc-port` and `k-orc-server` objects and let
  Argo re-create them.
- **Image**: pinned to CirrOS 0.6.3 (URL + sha256) in
  `resources/11-image.yaml`. Bump both together using the published
  `SHA256SUMS`.
- **Flavor**: `m1.xsmall`, 1024 MiB / 2 vCPUs / 15 GiB in
  `resources/10-flavor.yaml`.

## Pruning

Pruning is intentionally disabled (`Prune=false`) on all three Applications:
GitOps changes to the manifests do not delete OpenStack resources, and
neither CRDs nor the controller are removed on manifest removal. Deleting
the Applications is the supported way to remove everything (see above).

## Namespaces

- `orc-system`: ORC controller (created by the chart applications).
- `openstack`: ORC custom resources, the credentials Job and its Secret.
- The Argo CD namespace hosting the Applications is discovered at runtime
  from the deployed pattern Applications; the manifests' hardcoded
  `namespace: vp-gitops` is only a documented default that the tests
  override.

## Running the tests

`test_k_orc.py` is collected through `tests/ci/test_standalone.py` (the
`k-orc` directory name is not a valid Python package name, so
`tests/ci/conftest.py` puts it on `sys.path` and `test_standalone.py`
star-imports it). Run as usual:

```bash
VP_HUBCONFIG=<kubeconfig> ./pattern.sh make run-ci-tests
```

Environment knobs: `VP_KORC_TEST_TIMEOUT` (default 900s),
`VP_KORC_POLL_INTERVAL` (default 5s), `VP_KORC_REPOURL` /
`VP_KORC_TARGET_REVISION` (override the k-orc-resources source). The first
deployment can take a few minutes (chart pulls, controller start, image
download, VM build), hence the larger default timeout compared to the rest
of the suite.

The tests apply the Applications and then assert:

- `k-orc-crds`, `k-orc-operator` and `k-orc-resources` applications are
  Synced and Healthy,
- the ORC CRDs are installed and the controller Deployment is Available,
- every smoke stack object is `Available=True` / `Progressing=False` with a
  populated `status.id`, failing fast on terminal error reasons,
- the server boots from the managed image and the port has a fixed IP.
