#!/usr/bin/env bash
# Count an exact CloudWatch Logs marker without relying on the AWS CLI's
# unbounded automatic pagination. FilterLogEvents can return a cycle of
# nextToken values for a completed SSM stream, so every page is fetched once
# and a repeated token is treated as the end of the reachable result set.

cloudwatch_marker_count() {
  if [ "$#" -ne 4 ]; then
    return 2
  fi

  local region="$1"
  local log_group="$2"
  local log_stream_prefix="$3"
  local marker="$4"
  local next_token=""
  local response=""
  local event_count=""
  local returned_token=""
  local seen_token=""
  local token_repeated=0
  local page
  local -a aws_args=()
  local -a seen_tokens=()

  for ((page = 1; page <= 128; page++)); do
    aws_args=(
      logs filter-log-events
      --region "$region"
      --log-group-name "$log_group"
      --log-stream-name-prefix "$log_stream_prefix"
      --filter-pattern "\"$marker\""
      --limit 1
      --no-paginate
      --cli-connect-timeout 5
      --cli-read-timeout 10
      --query '{count:length(events),next_token:nextToken}'
      --output json
    )
    if [ -n "$next_token" ]; then
      aws_args+=(--next-token "$next_token")
    fi
    response="$(aws "${aws_args[@]}" 2>/dev/null)" || return 1

    event_count="$(jq -er '.count | select(type == "number" and . >= 0 and floor == .)' <<<"$response")" || return 1
    if [ "$event_count" -gt 0 ]; then
      printf '%s\n' "$event_count"
      return 0
    fi

    returned_token="$(jq -er '.next_token // "" | select(type == "string")' <<<"$response")" || return 1
    token_repeated=0
    for seen_token in "${seen_tokens[@]+"${seen_tokens[@]}"}"; do
      if [ "$seen_token" = "$returned_token" ]; then
        token_repeated=1
        break
      fi
    done
    if [ -z "$returned_token" ] || [ "$token_repeated" -eq 1 ]; then
      printf '0\n'
      return 0
    fi
    seen_tokens+=("$returned_token")
    next_token="$returned_token"
  done

  return 1
}
