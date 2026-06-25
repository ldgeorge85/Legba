#!/usr/bin/env bash
# run_tests.sh — Run all Legba test suites and report results.
#
# Usage:
#   ./scripts/run_tests.sh              # Run all tests
#   ./scripts/run_tests.sh --quick      # Run only core regression tests
#   ./scripts/run_tests.sh --verbose    # Run with verbose pytest output
#
# Prerequisites:
#   PYTHONPATH must include src/ (set automatically below).
#   pip install pytest pyyaml pynacl  (test dependencies)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONPATH="${PROJECT_ROOT}/src:${PYTHONPATH:-}"

# Parse flags
VERBOSE=""
QUICK=false
for arg in "$@"; do
    case "$arg" in
        --verbose|-v) VERBOSE="-v" ;;
        --quick|-q)   QUICK=true ;;
        *)            echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# Color output helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo " Legba Test Runner"
echo "========================================"
echo "Project root: $PROJECT_ROOT"
echo "PYTHONPATH:   $PYTHONPATH"
echo ""

# Define test suites
# Core regression tests (run in --quick mode and full mode)
CORE_TESTS=(
    "tests/test_phase1_fixes.py"
    "tests/test_phase3.py"
    "tests/test_phase4.py"
    "tests/test_domain_config.py"
    "tests/test_a2a_server.py"
)

# Extended tests (run only in full mode)
EXTENDED_TESTS=(
    "tests/test_unit.py"
    "tests/test_dedup.py"
    "tests/test_cluster.py"
    "tests/test_cognitive.py"
    "tests/test_cycle_routing.py"
)

# Integration tests (require running services — skipped unless services are up)
INTEGRATION_TESTS=(
    "tests/test_integration.py"
    "tests/test_graph_age.py"
)

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()

run_suite() {
    local test_file="$1"
    local label="$2"
    local full_path="${PROJECT_ROOT}/${test_file}"

    if [[ ! -f "$full_path" ]]; then
        echo -e "  ${YELLOW}SKIP${NC}  ${test_file} (file not found)"
        SKIP_COUNT=$((SKIP_COUNT + 1))
        RESULTS+=("SKIP  ${test_file}")
        return
    fi

    echo -n "  Running ${test_file} ... "
    local output exit_code=0
    output=$(python -m pytest "$full_path" $VERBOSE --tb=short -q 2>&1) || exit_code=$?
    local last_line
    last_line=$(echo "$output" | tail -1)

    if echo "$last_line" | grep -qE "failed|error|ERROR"; then
        echo -e "${RED}FAIL${NC}  ($last_line)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        RESULTS+=("FAIL  ${test_file}: ${last_line}")
        if [[ -n "$VERBOSE" ]]; then
            echo "$output"
        fi
    elif echo "$last_line" | grep -qE "passed|no tests ran"; then
        echo -e "${GREEN}PASS${NC}  ($last_line)"
        PASS_COUNT=$((PASS_COUNT + 1))
        RESULTS+=("PASS  ${test_file}: ${last_line}")
    else
        # Unknown output — treat non-zero exit as failure
        if [[ "$exit_code" -ne 0 ]]; then
            echo -e "${RED}FAIL${NC}  (exit code $exit_code)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            RESULTS+=("FAIL  ${test_file}: exit $exit_code")
            if [[ -n "$VERBOSE" ]]; then
                echo "$output"
            fi
        else
            echo -e "${GREEN}PASS${NC}  ($last_line)"
            PASS_COUNT=$((PASS_COUNT + 1))
            RESULTS+=("PASS  ${test_file}: ${last_line}")
        fi
    fi
}

# --- Core tests ---
echo "Core regression tests:"
echo "----------------------------------------"
for t in "${CORE_TESTS[@]}"; do
    run_suite "$t" "core"
done
echo ""

# --- Extended tests ---
if [[ "$QUICK" == false ]]; then
    echo "Extended tests:"
    echo "----------------------------------------"
    for t in "${EXTENDED_TESTS[@]}"; do
        run_suite "$t" "extended"
    done
    echo ""

    # --- Integration tests (check if services are up) ---
    echo "Integration tests (require live services):"
    echo "----------------------------------------"
    if redis-cli -h "${REDIS_HOST:-localhost}" ping 2>/dev/null | grep -q PONG; then
        for t in "${INTEGRATION_TESTS[@]}"; do
            run_suite "$t" "integration"
        done
    else
        echo -e "  ${YELLOW}SKIP${NC}  (Redis not available — integration tests require running services)"
        SKIP_COUNT=$((SKIP_COUNT + ${#INTEGRATION_TESTS[@]}))
        for t in "${INTEGRATION_TESTS[@]}"; do
            RESULTS+=("SKIP  ${t} (services not running)")
        done
    fi
    echo ""
fi

# --- Summary ---
echo "========================================"
echo " Results Summary"
echo "========================================"
TOTAL=$((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))
echo -e "  ${GREEN}PASS${NC}: ${PASS_COUNT}"
echo -e "  ${RED}FAIL${NC}: ${FAIL_COUNT}"
echo -e "  ${YELLOW}SKIP${NC}: ${SKIP_COUNT}"
echo "  Total: ${TOTAL}"
echo ""

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "Failed suites:"
    for r in "${RESULTS[@]}"; do
        if [[ "$r" == FAIL* ]]; then
            echo "  - ${r}"
        fi
    done
    echo ""
    exit 1
else
    echo "All tests passed."
    exit 0
fi
