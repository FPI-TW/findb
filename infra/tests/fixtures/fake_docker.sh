#!/bin/sh
set -eu
state_dir="${FAKE_DOCKER_STATE:?}"
printf '%s\n' "$1" >> "${FAKE_DOCKER_LOG:?}"
operation="$1"
shift
if [ -n "${FAKE_SIGNAL_ON:-}" ] && [ "$operation" = "$FAKE_SIGNAL_ON" ]   && [ ! -e "${FAKE_SIGNAL_SENT:?}" ]; then
  : > "$FAKE_SIGNAL_SENT"
  kill -TERM "$PPID"
fi
case "$operation" in
  container)
    if [ "$1" = ls ]; then
      for entry in "$state_dir"/*; do [ ! -f "$entry" ] || basename "$entry"; done
      exit 0
    fi
    [ "$1" = inspect ]
    [ -f "$state_dir/$2" ]
    ;;
  image)
    [ "$1" = prune ]
    [ "$2" = -af ]
    ;;
  pull)
    exit 0
    ;;
  rm)
    [ "$1" != "--name" ] || shift
    [ "$1" != "-f" ] || shift
    name="$1"
    if [ ! -f "$state_dir/$name" ]; then
      for entry in "$state_dir"/*; do
        [ -f "$entry" ] || continue
        inode="$(ls -i "$entry" | awk '{print $1}')"
        if [ "$name" = "fake-$inode" ]; then name="${entry##*/}"; break; fi
      done
    fi
    rm -f "$state_dir/$name"
    ;;
  rename)
    mv "$state_dir/$1" "$state_dir/$2"
    ;;
  stop)
    [ "$1" = "--time" ]
    name="$3"
    [ "$(cat "$state_dir/$name")" != stop-fail ] || exit 1
    printf 'stopped\n' > "$state_dir/$name"
    ;;
  start)
    name="$1"
    [ "$(cat "$state_dir/$name")" != start-fail ] || exit 1
    printf 'running\n' > "$state_dir/$name"
    ;;
  run)
    detached=0
    name=""
    previous=""
    for argument in "$@"; do
      if [ "$argument" = -d ]; then detached=1; fi
      if [ "$previous" = --name ]; then name="$argument"; fi
      previous="$argument"
    done
    if [ "$detached" -eq 1 ]; then
      [ -n "$name" ]
      printf '%s\n' "${FAKE_CANDIDATE_STATUS:-running}" > "$state_dir/$name"
    else
      [ "${FAKE_PREFLIGHT_FAIL:-0}" != 1 ]
    fi
    ;;
  create)
    name=""
    previous=""
    for argument in "$@"; do
      if [ "$previous" = --name ]; then name="$argument"; fi
      previous="$argument"
    done
    [ -n "$name" ]
    printf 'created\n' > "$state_dir/$name"
    ;;
  inspect)
    [ "$1" = --format ]
    format="$2"
    name="$3"
    status="$(cat "$state_dir/$name")"
    case "$format" in
      *Config.Labels*)
        # Labels belong to an identity and survive rename. Older fixtures may
        # still identify an initial unaccepted container by its fixed name.
        container_id="$(ls -i "$state_dir/$name" | awk '{print "fake-" $1}')"
        case ",${FAKE_UNACCEPTED_IDS:-}," in
          *",$container_id,"*) printf 'false\n'; exit 0 ;;
        esac
        case ",${FAKE_UNACCEPTED_NAMES:-}," in
          *",$name,"*) printf 'false\n' ;;
          *) printf '<no value>\n' ;;
        esac
        ;;
      *Config.Image*) printf '%s\n' "${FAKE_IMAGE:?}" ;;
      *Config.User*) printf '10001:10001\n' ;;
      *HostConfig.RestartPolicy.Name*) printf 'no\n' ;;
      *State.Running*) [ "$status" = running ] && printf 'true\n' || printf 'false\n' ;;
      *State.Status*) printf '%s\n' "$status" ;;
      *State.ExitCode*) printf '%s\n' "${FAKE_STABLE_EXIT_CODE:-0}" ;;
      *State.OOMKilled*) printf 'false\n' ;;
      *State.Error*) printf '\n' ;;
      *'{{.Id}}'*)
        # Container identity survives rename and state changes, unlike status.
        ls -i "$state_dir/$name" | awk '{print "fake-" $1}'
        ;;
      *RestartCount*) printf '0\n' ;;
      *) printf '%s\n' "$status" ;;
    esac
    ;;
  *)
    exit 2
    ;;
esac
