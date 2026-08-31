#!/usr/bin/env bash
# Read one AWS-RunShellScript invocation and accept only its exact terminal
# marker protocol. CloudWatch remains the complete command-output audit sink;
# this helper deliberately gates on the bounded SSM invocation response.

ssm_command_marker_state() {
  if [ "$#" -ne 5 ]; then
    echo "usage: ssm_command_marker_state REGION COMMAND_ID INSTANCE_ID SUCCESS_MARKER FAILED_MARKER" >&2
    return 2
  fi

  local region="$1"
  local command_id="$2"
  local instance_id="$3"
  local success_marker="$4"
  local failed_marker="$5"
  local response=""
  local state=""
  local aws_stderr=""

  aws_stderr="$(mktemp)" || return 1
  if ! response="$(aws ssm get-command-invocation \
    --region "$region" \
    --command-id "$command_id" \
    --instance-id "$instance_id" \
    --cli-connect-timeout 5 \
    --cli-read-timeout 10 \
    --query '{command_id:CommandId,instance_id:InstanceId,document_name:DocumentName,plugin_name:PluginName,status:Status,status_details:StatusDetails,response_code:ResponseCode,stdout:StandardOutputContent}' \
    --output json 2>"$aws_stderr")"; then
    cat "$aws_stderr" >&2
    if grep -Fq "InvocationDoesNotExist" "$aws_stderr"; then
      rm -f -- "$aws_stderr"
      printf 'pending\n'
      return 0
    fi
    rm -f -- "$aws_stderr"
    return 1
  fi
  rm -f -- "$aws_stderr"

  state="$(jq -er \
    --arg command_id "$command_id" \
    --arg instance_id "$instance_id" \
    --arg success_marker "$success_marker" \
    --arg failed_marker "$failed_marker" '
      def protocol_error: "protocol";
      def terminal_error: ["terminal", .status, .status_details, (.response_code | tostring)] | @tsv;
      if type != "object"
        or (.command_id | type) != "string"
        or (.instance_id | type) != "string"
        or (.document_name | type) != "string"
        or (.plugin_name | type) != "string"
        or (.status | type) != "string"
        or (.status_details | type) != "string"
        or (.response_code | type) != "number"
        or (.response_code | floor) != .response_code
        or (.stdout | type) != "string"
      then protocol_error
      elif .command_id != $command_id
        or .instance_id != $instance_id
        or .document_name != "AWS-RunShellScript"
        or .plugin_name != "aws:runShellScript"
      then protocol_error
      elif .status == "Success" then
        if .status_details != "Success" or .response_code != 0 then terminal_error
        elif ([.stdout | split("\n")[] | rtrimstr("\r") | select(. == $success_marker)] | length) != 1
          or ([.stdout | split("\n")[] | rtrimstr("\r") | select(. == $failed_marker)] | length) != 0
        then protocol_error
        else "success"
        end
      elif .status == "Pending" or .status == "InProgress" or .status == "Delayed" or .status == "Cancelling" then
        "pending"
      else terminal_error
      end
    ' <<<"$response")" || {
      echo "SSM invocation marker protocol failure: response shape validation failed" >&2
      return 1
    }

  case "$state" in
    pending|success)
      printf '%s\n' "$state"
      ;;
    protocol)
      echo "SSM invocation marker protocol failure: response identity or marker validation failed" >&2
      return 1
      ;;
    terminal$'\t'*)
      IFS=$'\t' read -r _ status status_details response_code <<<"$state"
      echo "SSM terminal invocation failure: status=$status status_details=$status_details response_code=$response_code" >&2
      return 1
      ;;
    *)
      echo "SSM invocation marker protocol failure: helper produced an invalid state" >&2
      return 1
      ;;
  esac
}
