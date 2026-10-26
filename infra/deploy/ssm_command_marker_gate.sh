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
    --query '{command_id:CommandId,instance_id:InstanceId,document_name:DocumentName,plugin_name:PluginName,status:Status,status_details:StatusDetails,response_code:ResponseCode,stdout:StandardOutputContent,stderr:StandardErrorContent}' \
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
        or (.stderr != null and (.stderr | type) != "string")
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
      # Surface only the allowlisted diagnostic protocol, never raw stdout/stderr.
      local diagnostic profile provider line
      diagnostic="$(jq -r '[.stdout | split("\n")[] | rtrimstr("\r") | select(test("^fetcher_deployment_blocked reason=scheduler_not_stopped execution_profile=(pilot|full_market) provider=(twelve_data|finlab|shioaji|taifex|factset)$"))] | unique | .[:8][]' <<<"$response")"
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        profile="${line#*execution_profile=}"; profile="${profile%% *}"
        provider="${line##*provider=}"
        echo "Fetcher deployment blocked: scheduler_not_stopped execution_profile=$profile provider=$provider. Stop the scheduler in Dashboard and wait for desired/observed stopped before retrying." >&2
        if [ "${GITHUB_ACTIONS:-false}" = true ]; then
          printf '::error title=Fetcher scheduler 尚未停止::execution_profile=%s provider=%s；請先在 Dashboard 停止該 scheduler，等待 desired/observed stopped 後重試。\n' "$profile" "$provider" >&2
        fi
      done <<<"$diagnostic"
      diagnostic="$(jq -r '[.stdout | split("\n")[] | rtrimstr("\r") | select(test("^fetcher_host_blocked reason=runtime_not_eligible execution_profile=(pilot|full_market|historical) provider=(twelve_data|finlab|shioaji|taifex|factset)$"))] | unique | .[:15][]' <<<"$response")"
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        profile="${line#*execution_profile=}"; profile="${profile%% *}"
        provider="${line##*provider=}"
        echo "Fetcher host preflight blocked: runtime_not_eligible execution_profile=$profile provider=$provider. Inspect the installed consumer and safely recover its original owner before retrying; keep its scheduler stopped." >&2
        if [ "${GITHUB_ACTIONS:-false}" = true ]; then
          printf '::error title=Fetcher 主機 runtime 未通過檢查::execution_profile=%s provider=%s；請檢查既有 consumer，安全復原原 owner，保持 scheduler 停止後重試。\n' "$profile" "$provider" >&2
        fi
      done <<<"$diagnostic"
      # Host scripts end each handled failure with a fixed "<component>=failed
      # reason=<token>" line, mostly on stderr. Surface only that exact grammar,
      # in first-seen order, so Docker/provider text never reaches the runner log.
      diagnostic="$(jq -r '[(.stdout, (.stderr // "")) | split("\n")[] | rtrimstr("\r") | select(test("^(application_environment|fetcher_aws_deploy|fetcher_checkpoint_gate|fetcher_runtime_inventory|findb_aws_deploy|install_findb_bootstrap|release_fetcher_provider|render_nginx_runtime|runtime_secret_command)=failed reason=[a-z0-9_]+( (runtime|container|service)=[a-z0-9][a-z0-9_.-]*| committed=true)*$"))] | reduce .[] as $line ([]; if index([$line]) then . else . + [$line] end) | .[:8][]' <<<"$response")"
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        echo "Remote deployment failure: $line" >&2
        if [ "${GITHUB_ACTIONS:-false}" = true ]; then
          printf '::error title=遠端部署失敗原因::%s\n' "$line" >&2
        fi
      done <<<"$diagnostic"
      return 1
      ;;
    *)
      echo "SSM invocation marker protocol failure: helper produced an invalid state" >&2
      return 1
      ;;
  esac
}

ssm_cancel_and_confirm() {
  local region="$1" command_id="$2" instance_id="$3" state deadline
  # Do not assume that a cancelled runner stopped the remote process. A terminal
  # SSM status is required; otherwise explicitly report the unresolved command.
  aws ssm cancel-command --region "$region" --command-id "$command_id" \
    --instance-ids "$instance_id" --cli-connect-timeout 5 --cli-read-timeout 10 \
    >/dev/null 2>&1 || true
  deadline=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$deadline" ]; do
    state="$(aws ssm get-command-invocation --region "$region" --command-id "$command_id" \
      --instance-id "$instance_id" --cli-connect-timeout 5 --cli-read-timeout 10 \
      --query Status --output text)" || break
    case "$state" in
      Success|Failed|Cancelled|TimedOut)
        echo "SSM remote command terminal after abort: command_id=$command_id status=$state. Verify host recovery before retrying." >&2
        return 0 ;;
      Pending|InProgress|Delayed|Cancelling) sleep 5 ;;
      *) break ;;
    esac
  done
  echo "SSM remote termination unconfirmed: command_id=$command_id. Inspect the host and CloudWatch before retrying; acceptance is blocked." >&2
  return 1
}

ssm_wait_command() (
  if [ "$#" -ne 6 ] || ! [[ "$6" =~ ^[1-9][0-9]*$ ]]; then
    echo 'usage: ssm_wait_command REGION COMMAND_ID INSTANCE_ID SUCCESS_MARKER FAILED_MARKER TIMEOUT_SECONDS' >&2
    return 2
  fi
  local region="$1" command_id="$2" instance_id="$3" success_marker="$4" failed_marker="$5"
  local deadline=$((SECONDS + $6)) state completed=false
  trap 'if [ "$completed" != true ]; then ssm_cancel_and_confirm "$region" "$command_id" "$instance_id" || true; fi' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  while [ "$SECONDS" -lt "$deadline" ]; do
    state="$(ssm_command_marker_state "$region" "$command_id" "$instance_id" "$success_marker" "$failed_marker")" || return 1
    [ "$SECONDS" -lt "$deadline" ] || break
    if [ "$state" = success ]; then
      completed=true
      return 0
    fi
    sleep 5
  done
  echo "SSM command deadline exceeded: command_id=$command_id timeout_seconds=$6; acceptance is blocked." >&2
  return 1
)
