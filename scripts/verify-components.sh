#!/bin/bash

# Simple Component Verification Script
# Verifies that all components in node.json have existing files
# Usage: ./scripts/verify-components.sh

set -e

echo "Verifying component paths from node.json..."

# Check if node.json exists
if [ ! -f "node.json" ]; then
    echo "Error: node.json not found"
    exit 1
fi

# Extract and verify components
missing_components=()
TOTAL_CHECKED=0

# Process each component
while IFS='|' read -r component_name component_path; do
    TOTAL_CHECKED=$((TOTAL_CHECKED + 1))
    # Convert dot notation to file path
    file_path=$(echo "$component_path" | sed 's/\./\//g').py

    if [ -f "$file_path" ]; then
        echo "✓ $component_name: $file_path"
    else
        echo "✗ $component_name: $file_path (MISSING)"
        missing_components+=("$component_name")
    fi
done < <(python3 -c "
import json
with open('node.json', 'r') as f:
    data = json.load(f)
for name, comp in data.get('components', {}).items():
    if 'path' in comp:
        print(f'{name}|{comp[\"path\"]}')
")

echo

# Final result
if [ ${#missing_components[@]} -eq 0 ]; then
    echo "✅ All $TOTAL_CHECKED components are registered correctly"
    exit 0
else
    echo "❌ ${#missing_components[@]} out of $TOTAL_CHECKED components are missing:"
    for component in "${missing_components[@]}"; do
        echo "  - $component"
    done
    exit 1
fi