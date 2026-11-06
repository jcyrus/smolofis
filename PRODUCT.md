# SmolOfis

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Small-office administrators who may not be comfortable in a terminal. Developers
normally use shares, Git repositories, runners, and builds directly; the server
should remain unobtrusive during everyday work.

## Product Purpose

An approachable control surface for managing shared folders and access, and
diagnosing service problems on the SmolOfis infrastructure appliance.

## Operating Context

Administrators use a desktop browser or a phone, including over private remote
access. The server must remain useful without internet access. Its existing
Rust/Axum/Askama panel provides boot state, host telemetry, and core service health.

## Capabilities and Constraints

The administration console is being developed behind a flag disabled by default.
VM, native client, and lifecycle acceptance remain release gates. Centralized
accounts are intended for hosted services; workstation domain sign-in is deferred.
Existing telemetry is real; user/share administration is not connected yet.
An opt-in directory administrator sign-in preview protects panel pages and
telemetry; the console remains loopback-only pending security/deployment acceptance.
Optional local root commands enroll an existing directory administrator, bind
the configuration to the appliance, and inspect or lock console access offline.
An opt-in private HTTPS transport candidate uses a fixed enrolled loopback backend
and Tailscale Serve; live tailnet and deployment acceptance remain open.
Gitea browser offboarding may sign all browser users out for v1, as approved.

## Brand Commitments

SmolOfis: approachable, calm, and practical. Borrow umbrelOS's easy discovery and
DSM's organized administration. Use responsive full-page tools, not movable windows.

## Evidence on Hand

The existing panel and real state API, the device administration scripts, and
the identity-lab evidence in `docs/identity-prototype.md`. The container prototype
does not prove native client compatibility or a full kernel boot.

## Product Principles

- Explain what needs attention and the next useful action.
- Create an office identity once and grant explicit service access.
- Display confirmed state; distinguish unavailable capabilities from empty data.
- Make administrative actions understandable on desktop and mobile.
- Keep changes to the appliance behind appropriate authentication and privilege boundaries.
