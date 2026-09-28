#!/usr/bin/env bash

FLEETLINE_PG_VERSION=16.15
FLEETLINE_PG_SHA256=c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed
FLEETLINE_REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

fleetline_pg_bin() {
  if [[ -n "${POSTGRES_BIN_DIR:-}" && -x "${POSTGRES_BIN_DIR}/postgres" ]]; then
    printf '%s\n' "$POSTGRES_BIN_DIR"
  elif command -v postgres >/dev/null 2>&1; then
    dirname "$(command -v postgres)"
  else
    printf '%s\n' "$FLEETLINE_REPO_ROOT/.tools/postgresql-$FLEETLINE_PG_VERSION/bin"
  fi
}

fleetline_provision_postgres() {
  local install_dir="$FLEETLINE_REPO_ROOT/.tools/postgresql-$FLEETLINE_PG_VERSION"
  if [[ -x "$install_dir/bin/postgres" && -f "$install_dir/share/extension/pg_trgm.control" ]]; then
    return
  fi

  local cache_dir="$FLEETLINE_REPO_ROOT/.tools/downloads"
  local archive="$cache_dir/postgresql-$FLEETLINE_PG_VERSION.tar.bz2"
  mkdir -p "$cache_dir" "$FLEETLINE_REPO_ROOT/.tools"
  if [[ ! -f "$archive" ]] || ! printf '%s  %s\n' "$FLEETLINE_PG_SHA256" "$archive" | sha256sum -c - >/dev/null 2>&1; then
    curl --retry 8 --retry-all-errors --retry-delay 2 -fL \
      "https://ftp.postgresql.org/pub/source/v$FLEETLINE_PG_VERSION/postgresql-$FLEETLINE_PG_VERSION.tar.bz2" \
      -o "$archive"
  fi
  printf '%s  %s\n' "$FLEETLINE_PG_SHA256" "$archive" | sha256sum -c -

  local build_dir
  build_dir=$(mktemp -d "${TMPDIR:-/tmp}/fleetline-postgres-build.XXXXXX")
  tar -xjf "$archive" -C "$build_dir"
  if [[ ! -x "$install_dir/bin/postgres" ]]; then
    (
      cd "$build_dir/postgresql-$FLEETLINE_PG_VERSION"
      ./configure --prefix="$install_dir" --without-readline --without-zlib --without-icu
      make -j"$(getconf _NPROCESSORS_ONLN)"
      make install
    )
  fi
  if [[ ! -f "$install_dir/share/extension/pg_trgm.control" ]]; then
    (
      cd "$build_dir/postgresql-$FLEETLINE_PG_VERSION/contrib/pg_trgm"
      make USE_PGXS=1 PG_CONFIG="$install_dir/bin/pg_config" -j"$(getconf _NPROCESSORS_ONLN)"
      make USE_PGXS=1 PG_CONFIG="$install_dir/bin/pg_config" install
    )
  fi
  rm -rf -- "$build_dir"
}

fleetline_start_postgres() {
  local run_dir=$1
  local port=$2
  local database=$3
  fleetline_provision_postgres
  local bin
  bin=$(fleetline_pg_bin)
  mkdir -p "$run_dir/socket"
  "$bin/initdb" -D "$run_dir/data" --auth-local=trust --auth-host=trust --no-locale --encoding=UTF8 >/dev/null
  "$bin/pg_ctl" -D "$run_dir/data" -l "$run_dir/postgres.log" \
    -o "-h 127.0.0.1 -p $port -k $run_dir/socket -F -c fsync=off -c full_page_writes=off" -w start >/dev/null
  "$bin/createdb" -h 127.0.0.1 -p "$port" "$database"
  export POSTGRES_HOST=127.0.0.1
  export POSTGRES_PORT=$port
  export POSTGRES_DB=$database
  export POSTGRES_USER
  POSTGRES_USER=$(id -un)
  export POSTGRES_PASSWORD=
}

fleetline_stop_postgres() {
  local run_dir=$1
  local bin
  bin=$(fleetline_pg_bin)
  [[ -f "$run_dir/data/postmaster.pid" ]] && "$bin/pg_ctl" -D "$run_dir/data" -m fast -w stop >/dev/null
}
