# Test artifact only; build with --platform=linux/amd64 for the full-kernel VM.
# Context is src-dashboard, never the workstation home or private lab storage.
FROM rust:alpine AS build
RUN apk add --no-cache gcc musl-dev
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY templates ./templates
COPY assets ./assets
RUN rustc --version && cargo build --locked \
    --config profile.dev.debug=0 --config profile.dev.strip=true
FROM scratch
COPY --from=build /src/target/debug/smolofis-panel /smolofis-panel
