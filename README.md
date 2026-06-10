# 📦 SmolOfis

> The "smol" infrastructure appliance for agile web teams. No cloud tax. No enterprise bloat. Just pure developer autonomy.

SmolOfis is a custom, lightweight, headless Linux operating system appliance designed to turn any spare hardware (like an M-series Mac Mini or mini-PC) into a fully automated, self-hosted DevOps hub for small engineering teams. 

Instead of fighting complex configuration scripts or heavy enterprise monoliths, SmolOfis flashes as a single ISO, boots instantly, and exposes a high-performance management dashboard built entirely in Rust.

---

## 🛠️ The Tech Stack

- **OS Base:** Minimal, headless Debian/Ubuntu base system configured for appliance stability.
- **Control Plane:** Compiled Rust binary using `axum` (async web engine), `sysinfo` (telemetry), and `askama` (compiled, type-safe HTML templates) with Tailwind CSS.
- **Git & CI/CD:** Native Gitea integration paired with Gitea Actions for syntax-compatible GitHub pipeline workflows.
- **Orchestration (PaaS):** Coolify engine for managing automated branch-based builds, databases, and multi-server deployments.
- **Networking & Discovery:** Built-in Avahi (`smolofis.local` mDNS resolution) and native Tailscale/Cloudflare Tunnel support.

---

## 🏗️ Architecture & Boot Sequence

When you power on a machine running SmolOfis, it coordinates initialization gracefully via `systemd`:

1. **Kernel Bootstrap:** The stripped-down kernel boots and brings up core networking targets.
2. **Instant UI Panel:** A specialized systemd service launches the `smolofis-panel` Rust binary on port `80`. It immediately serves a responsive "System Initializing..." interface.
3. **Daemon Initialization:** Background systemd workers launch the Docker engine and network discovery layers (`avahi-daemon`).
4. **App Ecosystem Spin-up:** Docker automatically starts pre-configured Gitea, Coolify, and local storage orchestration layers.
5. **Dashboard Transition:** The Rust panel polls the core services locally. Once they pass health checks, the UI shifts smoothly to the operational management cockpit.

---

## 🚧 Project Status: Active Development (Ongoing)

⚠️ **Current Status: Alpha / Work-in-Progress**

SmolOfis is a highly transparent, **ongoing personal portfolio project** aimed at exploring platform engineering, system initialization, and infrastructure automation. 

- **What works right now:** The underlying system architecture design, the core Rust dashboard structure (`src-dashboard`), and the initial `systemd` service orchestration schema.
- **What is currently being coded:** The automated ISO generation pipelines (`scripts/build-image.sh`) and the type-safe API clients that bridge the Rust UI to Gitea and Coolify.

Because this project is actively evolving, breaking changes to the configuration structure are to be expected. Feature requests, architectural feedback, and code contributions are highly encouraged!

---

## 🗺️ Implementation Roadmap

### Phase 1: Core Dashboard (In Progress)
- [x] Scaffold workspace structure.
- [ ] Implement asynchronous service status polling using `tokio` and `reqwest`.
- [ ] Complete the Tailwind CSS dark-mode telemetry layout.

### Phase 2: OS Customization (Up Next)
- [ ] Build the `debootstrap` / `live-build` workflow configuration.
- [ ] Inject custom systemd configurations to manage boot sequencing.
- [ ] Lock down the default minimal package selection.

### Phase 3: The "One-Click" ISO
- [ ] Create a GitHub Actions workflow to compile the full OS image on every main-branch push.
- [ ] Release flashable `.iso` files directly via GitHub releases.

---

## 🤝 Contributing & Feedback

Since SmolOfis is a collaborative playground to showcase robust systems design, feel free to open an Issue or start a Discussion if you want to chat about:
- Enhancing the `systemd` boot optimization.
- Improving compilation times for the Rust web wrapper.
- Better approaches to sandboxing root filesystems.

Licensed under the MIT License. Built with 🦀 and passion for the independent web.
