# How the machine calculator works

The [BORG planner](https://borg.utlyze.com/#configure) estimates each machine from its own role and workload. It does not divide a fleet total between machines. Its versioned inputs and allowances live in [catalog.json](../platform/catalog.json), and the calculation is [readable JavaScript](../platform/planner.mjs).

These are **engineering planning allowances**, not benchmarks, certified minimums, agent-count guarantees or live dispatch admission. Start with the [hardware guide](HARDWARE.md) and measure a representative workload before purchasing or expanding a fleet. Apple Silicon macOS is the verified complete installation path; Linux and Intel macOS acceptance remains pending. Windows is unsupported.

## Inputs for each machine

| Input | Meaning |
|---|---|
| Full node / tools node | A full node runs the local memory, graph and default small models. A tools node starts its native connector and selected work services. Both install the complete dependency package. |
| Active agents | Simultaneously active cloud-agent clients with local tool work. Their provider model weights do not run on your machine. |
| Browsers | Concurrent independent browser sessions, not tabs in one shared physical desktop. |
| Builds/tests | Concurrent ordinary project compile/test jobs. Large monorepos, containers and VMs need additional measurement. |
| Stored facts | Millions of local memory facts, including a rough allowance for vectors, payload, graph and history. A tools node has no local brain, so this input does not increase its estimate. |
| Project storage | Current working data in GB. The calculator adds room for caches and working copies. |
| Extraction context | 8K, 16K or 32K for the pinned local extraction model; not your cloud agent's context window. |
| Training | A separate experimental workload with additional RAM and checkpoint space. Selecting it does not enable training or promote an adapter. |

## Transparent allowances

All disk values below are free SSD space. They exclude the operating system's occupied disk, off-machine backups, separately hosted external services and unrelated applications.

| Budget item | RAM allowance | Free disk allowance | CPU allowance |
|---|---:|---:|---:|
| OS reserve | 4 GB | — | 1 core |
| Full memory/model/graph services | 8 GB | 30 GB | 2 cores |
| Tools and selected work services, instead of full services | 2 GB | 20 GB | 1 core |
| Model context reserve on full nodes | 2 GB × context / 16,384 | — | — |
| Each active cloud-agent client | 1 GB | — | 0.25 core |
| Each browser session | 2 GB | — | 0.5 core |
| Each ordinary build/test job | 3 GB | — | 2 cores |
| Each million local facts | 6 GB | 20 GB | — |
| Project data | — | 1.5 × supplied GB | — |
| Optional adapter base/evaluation budget | 4 GB | 10 GB | — |
| Optional training experiment, in addition to the above | 32 GB | 100 GB | 2 cores |

The calculator adds **30% headroom**, then rounds RAM to a useful hardware tier, CPU to an even core count and free disk to 25 GB increments. It applies a planning floor of 32 GB RAM / 100 GB free disk for full nodes, 16 GB / 50 GB for tools nodes, and 64 GB / 200 GB for training. A combined workload can exceed those floors. This is why a default coding node with 50 GB of projects currently produces 32 GB RAM, 8 cores and 150 GB free SSD.

Cloud-agent count, browser count and build count are separate resource consumers; count a browser or build once in its own field. The full service allowance includes the shared extraction/graph model once. It does not load a separate frontier model for each provider. Training assumes daily services remain running, so dedicated training-only deployments may have different needs.

The fact allowance is our combined storage estimate, not an upstream database formula. A million 768-dimensional float32 vectors alone occupy approximately 3.07 GB before indexes, payload, graph/history and backups. Real retention, payload sizes, index settings and working sets vary substantially. See [Qdrant capacity planning](https://qdrant.tech/documentation/operations/capacity-planning/).

Ollama documents that model concurrency and context length affect memory consumption. BORG's default local inference remains one parallel request and at most two loaded models. Increasing cloud-agent count in this calculator does not increase Ollama's parallelism. See [Ollama concurrency guidance](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests) and the [locked BORG configuration](HARDWARE.md#exact-default-model-and-service-configuration).

## What the result does not include

External integrations are assumed to run elsewhere. Connecting to your existing n8n, CRM, database or observability service uses that service's resources. If you host it on a BORG machine, add its own measured or upstream-recommended CPU, RAM and storage requirements before choosing hardware. Media rendering, CAD, scientific jobs and local frontier-model inference also require their own budgets.

A sum of fleet RAM is not available to one local model. Memory is not replicated by connector enrollment. One physical desktop still has one foreground focus. Provider allowance, network speed, storage I/O, GPU compatibility and operating-system permissions are separate from this calculator. On non-Apple hardware, verify actual Ollama GPU support and model residency; the RAM estimate is not a GPU-memory certification.

The downloaded blueprint contains workload and component selections only. Hardware estimates are advisory and are not used to authorize a dispatch, bypass capacity checks or enable an unverified provider. Run `borg doctor`, inspect live machine pressure and service backlog, and verify the actual task after installation.
