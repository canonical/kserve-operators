vLLM
====

`vLLM`_ is a high-throughput inference engine for LLMs. It runs every model in the stack
and exposes the OpenAI-compatible API that clients call.

Role in the stack
-----------------

``llm-integrator`` runs vLLM in its worker pods, from a CPU or a CUDA image depending on
the ``accelerator`` option. Both images are published by Canonical; you can override them
with the ``runtime-image`` option.

The charm sets the vLLM arguments it manages, for example
``--tensor-parallel-size`` from ``gpu-count`` and ``--max-model-len`` from
``max-model-len``. Other arguments can be added with ``vllm-extra-args``, which rejects
the managed flags as well as ``--host``, ``--port`` and ``--kv-transfer-config``.

Every worker exports Prometheus metrics with the ``vllm:`` prefix. The dashboards and
autoscaling read them, see :ref:`reference-observability`.

Further reading
---------------

* `vLLM documentation <https://docs.vllm.ai/>`_
* `vLLM metrics <https://docs.vllm.ai/en/latest/design/metrics/>`_
* `OpenAI-compatible server <https://docs.vllm.ai/en/latest/serving/openai_compatible_server/>`_
