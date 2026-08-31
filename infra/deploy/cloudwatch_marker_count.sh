#!/usr/bin/env bash
# Count an exact CloudWatch Logs marker from one complete SSM output stream.
# GetLogEvents requires manual forward pagination: a page is terminal only
# when its nextForwardToken equals the token supplied for that request.
# Any other token cycle, malformed response, or page-bound exhaustion fails
# closed so the caller can retry within its bounded SSM-success polling loop.

cloudwatch_marker_count() {
  if [ "$#" -ne 4 ]; then
    return 2
  fi

  local region="$1"
  local log_group="$2"
  local log_stream_name="$3"
  local marker="$4"
  local next_token=""
  local response=""
  local event_count=""
  local returned_token=""
  local seen_token=""
  local has_next_token=0
  local page
  local -a aws_args=()
  local -a seen_tokens=()

  for ((page = 1; page <= 128; page++)); do
    aws_args=(
      logs get-log-events
      --region "$region"
      --log-group-name "$log_group"
      --log-stream-name "$log_stream_name"
      --start-from-head
      --no-paginate
      --cli-connect-timeout 5
      --cli-read-timeout 10
      --query '{events:events,next_forward_token:nextForwardToken}'
      --output json
    )
    if [ "$has_next_token" -eq 1 ]; then
      aws_args+=(--next-token "$next_token")
    fi
    response="$(aws "${aws_args[@]}" 2>/dev/null)" || return 1

    event_count="$(jq -er --arg marker "$marker" '
      if type != "object" or (.events | type) != "array"
        or ([.events[] | type == "object" and (.message | type) == "string"] | all | not)
      then error("malformed GetLogEvents response")
      else [.events[] | select(.message == $marker)] | length
      end
    ' <<<"$response")" || return 1
    returned_token="$(jq -er '.next_forward_token | select(type == "string" and length > 0)' <<<"$response")" || return 1
    if [ "$event_count" -gt 0 ]; then
      printf '%s\n' "$event_count"
      return 0
    fi

    if [ "$has_next_token" -eq 1 ] && [ "$returned_token" = "$next_token" ]; then
      printf '0\n'
      return 0
    fi
    for seen_token in "${seen_tokens[@]+"${seen_tokens[@]}"}"; do
      if [ "$seen_token" = "$returned_token" ]; then
        return 1
      fi
    done
    seen_tokens+=("$returned_token")
    next_token="$returned_token"
    has_next_token=1
  done

  return 1
}
