#!/bin/bash
#
# Verify worktree test results
# Run this after executing: bro submit workers/test-worktree-manager.json
#
# Usage:
#   ./scripts/verify_worktree_test.sh [--source <path>]
#
# Options:
#   --source, -s  Git worktree working directory (same as bro submit --source)
#
# This script checks:
# 1. Worktrees created during test
# 2. Branches created
# 3. Files created/modified
# 4. Conflict state (if any)
# 5. Cleanup status
#

set -e

SOURCE_DIR=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --source|-s)
            SOURCE_DIR="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--source <path>]"
            echo ""
            echo "Options:"
            echo "  --source, -s  Git worktree working directory"
            echo "  --help, -h    Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

if [ -n "$SOURCE_DIR" ]; then
    if [ ! -d "$SOURCE_DIR" ]; then
        echo "ERROR: Source directory does not exist: $SOURCE_DIR"
        exit 1
    fi
    cd "$SOURCE_DIR"
    echo "Working directory: $SOURCE_DIR"
fi

echo "=============================================="
echo "Git Worktree Test Verification"
echo "=============================================="
echo ""

# Check if in git repo
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "ERROR: Not in a git repository"
    echo "Current directory: $(pwd)"
    exit 1
fi

echo "1. Current Git State"
echo "--------------------------------------------"
echo "Current branch: $(git branch --show-current)"
echo "HEAD commit: $(git rev-parse --short HEAD)"
echo ""

echo "2. All Worktrees"
echo "--------------------------------------------"
git worktree list
echo ""

echo "3. Test-related Branches"
echo "--------------------------------------------"
git branch -a | grep -i "worktree\|test-worktree" || echo "(No test branches found)"
echo ""

echo "4. Test Fixture Files"
echo "--------------------------------------------"
if [ -d "tests/fixtures/worktree" ]; then
    echo "Directory: tests/fixtures/worktree/"
    ls -la tests/fixtures/worktree/ 2>/dev/null || echo "(empty)"
    echo ""
    
    if [ -f "tests/fixtures/worktree/shared_config.json" ]; then
        echo "shared_config.json content:"
        cat tests/fixtures/worktree/shared_config.json
        echo ""
    fi
    
    for f in tests/fixtures/worktree/*_only.txt; do
        if [ -f "$f" ]; then
            echo "$(basename $f): $(cat $f)"
        fi
    done
else
    echo "(Directory does not exist)"
fi
echo ""

echo "5. Git Status (Conflict Check)"
echo "--------------------------------------------"
git_status=$(git status --porcelain)
if [ -z "$git_status" ]; then
    echo "Working directory clean - no conflicts"
else
    echo "Changes detected:"
    echo "$git_status"
    
    # Check for conflict markers
    if git status | grep -q "Unmerged paths"; then
        echo ""
        echo "⚠️  CONFLICTS DETECTED:"
        git status | grep "both modified"
    fi
fi
echo ""

echo "6. Worktree Cleanup Verification"
echo "--------------------------------------------"
# Check for orphaned worktree entries
orphaned_count=$(git worktree list --porcelain | grep "^worktree " | while read line; do
    wt_path="${line#worktree }"
    if [ ! -d "$wt_path" ]; then
        echo "orphaned"
    fi
done | wc -l)

if [ "$orphaned_count" -gt 0 ]; then
    echo "Found $orphaned_count orphaned worktree entries"
    echo "Run 'git worktree prune' to clean up"
else
    echo "No orphaned worktree entries"
fi
echo ""

echo "7. Recent Commits (last 10)"
echo "--------------------------------------------"
git log --oneline -10
echo ""

echo "=============================================="
echo "Verification Complete"
echo "=============================================="

# Summary
total_worktrees=$(git worktree list | wc -l)
test_branches=$(git branch -a | grep -c -i "worktree\|test-worktree" || echo "0")

echo ""
echo "Summary:"
echo "  Total worktrees: $total_worktrees"
echo "  Test branches: $test_branches"

if [ -f "tests/fixtures/worktree/shared_config.json" ]; then
    echo "  Fixture file: EXISTS"
else
    echo "  Fixture file: MISSING"
fi
