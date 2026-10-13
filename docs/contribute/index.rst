.. _contribute:

Contribute
==========

Charmed KServe is developed in the open on GitHub, in the `kserve-operators`_
repository. The Terraform solution lives in the `autoscaling-model-serving`_ repository.

Report a problem
----------------

`Open an issue <https://github.com/canonical/kserve-operators/issues>`_ for bugs in the
charms or in this documentation. Do not report security issues there: report them
privately through `Launchpad <https://wiki.ubuntu.com/DebuggingSecurity#How%20to%20File>`_.

Contribute code
---------------

The repository's ``CONTRIBUTING.md`` explains how to set up a development environment,
build the charms and run their tests. Contributions require signing the `Canonical
contributor licence agreement <https://canonical.com/legal/contributors>`_.

Contribute to the documentation
-------------------------------

The documentation is in the ``docs`` directory of the repository and is built with the
`Sphinx Stack <https://github.com/canonical/sphinx-stack>`_. To preview your changes,
run from that directory:

.. code-block:: bash

   make run

Then open http://127.0.0.1:8000. Before opening a pull request, run the checks:

.. code-block:: bash

   make spelling
   make linkcheck
   make woke

Versions and shared links are defined once, in the ``docs/reuse`` directory. Update
``docs/reuse/substitutions.txt`` when a release changes a version.
