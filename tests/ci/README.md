# RHOSO deployed-cluster CI tests

These tests are read-only and require an already deployed RHOSO pattern.

Run them with the pattern framework:

```bash
VP_HUBCONFIG="$HOME/.kube/config" ./pattern.sh make run-ci-tests
```

The framework runs `tests/ci/test_standalone.py` and writes JUnit XML to
`tests/ci/.results/test_standalone.xml`.

Configuration:

- `VP_HUBCONFIG`: kubeconfig for the managing cluster.
- `VP_RHOSO_TEST_TIMEOUT`: shared read-only assertion deadline in seconds.
- `VP_RHOSO_POLL_INTERVAL`: polling interval in seconds.

The expected Application inventory and sources are rendered from the public
chart and checked-in values on every test run. The managing ArgoCD namespace
is discovered from the deployed framework Applications.

The tests do not create, mutate, or delete cluster resources.
