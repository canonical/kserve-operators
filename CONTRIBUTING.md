## How to Manage Python Dependencies and Environments


### Prerequisites

`tox` is the only tool required locally, as `tox` internally installs and uses `poetry`, be it to manage Python dependencies or to run `tox` environments. To install it: `pipx install tox`.

Optionally, `poerty` can be additionally installed independently just for the sake of running Python commands locally outside of `tox` during debugging/development. To install it: `pipx install poetry`.


### Updating Dependencies

To add/update/remove any dependencies and/or to upgrade Python, simply:

1. add/update/remove such dependencies to/in/from the desired group(s) below `[tool.poetry.group.<your-group>.dependencies]` in `pyproject.toml`, and/or upgrade Python itself in `requires-python` under `[project]`

    _⚠️ dependencies for the charm itself are also defined as dependencies of a dedicated group called `charm`, specifically below `[tool.poetry.group.charm.dependencies]`, and not as project dependencies below `[project.dependencies]` or `[tool.poetry.dependencies]` ⚠️_

2. run `tox -e update-requirements` to update the lock file

    by this point, `poerty`, through `tox`, will let you know if there are any dependency conflicts to solve.

3. optionally, if you also want to update your local environment for running Python commands/scripts yourself and not through tox, see [Running Python Environments](#running-python-environments) below


### Upgrading Upstream KServe Version

When upgrading upstream KServe content used by charm templates (for example from `v0.17.0` to `v0.18.0`), use the `# Source:` comments above each Kubernetes object as your single source of truth.

#### Upgrade Flow (example: `v0.17.0` -> `v0.18.0`)

1. define your target tag (for example: `v0.18.0`) and confirm it exists in upstream KServe

2. inspect the object-level upstream references currently used by both charms:

    ```bash
    grep -R -n '^# Source:' charms/kserve-controller/src/templates charms/kserve-llmisvc/src/templates
    ```

3. for each `# Source:` URL, compare source content between old and new tags in upstream (replace `<path-from-url>` with the path after `/blob/<tag>/`):

    ```bash
    git -C kserve diff v0.17.0..v0.18.0 -- <path-from-url>
    ```

4. update the corresponding object in the charm template, preserving charm-specific substitutions (for example Jinja variables like `{{ namespace }}` and image overrides)

5. update the `# Source:` URL above that object to point to the new tag (`v0.18.0`)

6. if an upstream file moved, split, or was removed:
    - locate the new upstream file in `kserve/`
    - update the object and `# Source:` URL to that new specific file
    - if an object is no longer present upstream, document why it remains (or remove it)

7. verify every object still has exactly one `# Source:` line above it:

    ```bash
    for f in $(find charms/kserve-controller/src/templates charms/kserve-llmisvc/src/templates -type f -name '*.yaml.j2' | sort); do
      obj=$(grep -nE '^(apiVersion:|\{% raw %\}apiVersion:)' "$f" | wc -l)
      src=$(grep -nE '^# Source:' "$f" | wc -l)
      echo "$obj objects / $src sources :: $f"
    done
    ```

8. run validation before committing:

    ```bash
    tox -c charms/kserve-controller -e lint
    tox -c charms/kserve-controller -e unit
    tox -c charms/kserve-llmisvc -e lint
    tox -c charms/kserve-llmisvc -e unit
    ```

#### Scope Guidance

- Update templates that track upstream manifests:
  - `charms/kserve-controller/src/templates/*.yaml.j2`
  - `charms/kserve-llmisvc/src/templates/*.yaml.j2`
- Do not force upstream links for charm-local files that are not copied from upstream manifests (for example `ssl.conf.j2`).


### Running `tox` Environments

To run `tox` environments, either locally for development or in CI workflows for testing, ensure to have `tox` installed first and then simply run your `tox` environments natively (e.g.: `tox -e lint`). `tox` will internally first install `poetry` and then rely on it to install and run its environments.


### Running Python Environments

To run Python commands locally for debugging/development from any environments built from any combinations of dependency groups without relying on `tox`:
1. ensure you have `poetry` installed
2. install any required dependency groups: `poetry install --only <your-group-a>,<your-group-b>` (or all groups, if you prefer: `poetry install --all-groups`)
3. run Python commands via poetry: `poetry run python3 <your-command>`
