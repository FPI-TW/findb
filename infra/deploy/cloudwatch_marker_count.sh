#!/usr/bin/env bash
# Count an exact marker line from one complete SSM CloudWatch output stream.
# CloudWatch may pack several stdout lines into one event or split one line
# across adjacent events/pages. Reconstruct the ordered stream before comparing
# complete lines instead of requiring any individual message to match.
# Callers invoke this helper only after SSM reports Success, so an exact current
# tail fragment is complete even while CloudWatch's forward token is unsettled.
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
  local page_analysis=""
  local event_count=""
  local line_fragment=""
  local normalized_fragment=""
  local marker_count=0
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
    # Preserve AWS CLI diagnostics on stderr. Callers capture only stdout for
    # the numeric count, so authorization and service failures remain visible
    # without contaminating the marker result.
    response="$(aws "${aws_args[@]}")" || return 1

    page_analysis="$(jq -cer --arg marker "$marker" --arg fragment "$line_fragment" '
      if type != "object" or (.events | type) != "array"
        or ([.events[] | type == "object" and (.message | type) == "string"] | all | not)
      then error("malformed GetLogEvents response")
      else
        ($fragment + ([.events[].message] | join(""))) as $text
        | ($text | split("\n")) as $lines
        | {
            event_count: (
              (if ($text | endswith("\n")) then $lines else $lines[0:-1] end)
              | map(rtrimstr("\r"))
              | map(select(. == $marker))
              | length
            ),
            line_fragment: (
              if ($text | endswith("\n")) then "" else ($lines[-1] // "") end
            )
          }
      end
    ' <<<"$response")" || return 1
    event_count="$(jq -er '.event_count | select(type == "number" and . >= 0)' <<<"$page_analysis")" || return 1
    line_fragment="$(jq -er '.line_fragment | select(type == "string")' <<<"$page_analysis")" || return 1
    marker_count=$((marker_count + event_count))
    normalized_fragment="${line_fragment%$'\r'}"
    if [ "$normalized_fragment" = "$marker" ]; then
      marker_count=$((marker_count + 1))
    fi
    returned_token="$(jq -er '.next_forward_token | select(type == "string" and length > 0)' <<<"$response")" || return 1
    if [ "$marker_count" -gt 0 ]; then
      printf '%s\n' "$marker_count"
      return 0
    fi

    if [ "$has_next_token" -eq 1 ] && [ "$returned_token" = "$next_token" ]; then
      printf '%s\n' "$marker_count"
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
