import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

# ==========================================
# Part 1: Stroke Counting Logic (from Stock_count_v1.py)
# ==========================================

def load_stroke_dict(filepath: str):
    stroke_dict = {}
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    char = parts[0].strip()
                    try:
                        stroke = int(parts[1])
                        stroke_dict[char] = stroke
                    except ValueError:
                        continue
    except Exception as e:
        print(f"Error loading dictionary {filepath}: {e}")
        sys.exit(1)
    return stroke_dict

def get_stroke_count(char: str, stroke_dict):
    """Get the stroke count; if not available, use the Unicode value instead"""
    if char in stroke_dict:
        return stroke_dict[char]
    else:
        # Fallback to unicode if missing
        return ord(char)

def word_sort_key(word: str, stroke_dict):
    """
    Generate a sorting key for a single word:
    - Group by first character (stroke count + unicode)
    - Then compare remaining strokes character by character
    - Then compare remaining unicodes
    """
    if not word:
        return ((-1, -1), [], [])

    strokes = [get_stroke_count(c, stroke_dict) for c in word]
    unicodes = [ord(c) for c in word]
    
    # Primary sort: First character's stroke count and unicode
    # This ensures all streets starting with the same character are grouped together
    first_char_key = (strokes[0], unicodes[0])
    
    return (first_char_key, strokes[1:], unicodes[1:])

def check_missing_chars(words, stroke_dict):
    """Check which characters in the word list are missing stroke counts"""
    missing = set()
    for w in words:
        for c in w:
            if c not in stroke_dict:
                missing.add(c)

    if missing:
        print("\n=== Missing character check ===")
        print(f"Found {len(missing)} characters without stroke counts (will use Unicode fallback).")
        print(" ".join(sorted(missing)))
    else:
        print("\nAll characters have stroke counts defined.")

# ==========================================
# Part 2: Batch SQL Generation (Adapted from fast_batch_sql_generator.py)
# ==========================================

def group_streets_by_first_character(street_data, stroke_dict):
    """Group streets by first character stroke count, then sort within each group."""
    
    # Create groups based on first character
    groups = {}
    
    for tc_street, sc_street in street_data:
        # Use tc_street for grouping, fallback to sc_street if tc is empty
        street_for_grouping = tc_street if tc_street and tc_street.strip() and tc_street != 'NULL' else sc_street
        
        if street_for_grouping and street_for_grouping.strip() and street_for_grouping != 'NULL':
            try:
                first_char = street_for_grouping[0]
                # Use our get_stroke_count instead of strokes package
                first_char_strokes = get_stroke_count(first_char, stroke_dict)
                
                # Group by first character stroke count
                if first_char_strokes not in groups:
                    groups[first_char_strokes] = []
                groups[first_char_strokes].append((tc_street, sc_street))
                
            except:
                # Fallback group
                if 'unknown' not in groups:
                    groups['unknown'] = []
                groups['unknown'].append((tc_street, sc_street))
        else:
            # Group for empty/null values
            if 'empty' not in groups:
                groups['empty'] = []
            groups['empty'].append((tc_street, sc_street))
    
    # Sort groups by stroke count
    sorted_groups = []
    
    # First add groups with numeric stroke counts (sorted)
    numeric_groups = [(k, v) for k, v in groups.items() if isinstance(k, int)]
    numeric_groups.sort(key=lambda x: x[0])  # Sort by stroke count
    
    for stroke_count, group_streets in numeric_groups:
        sorted_groups.append((stroke_count, group_streets))
    
    # Add special groups at the end
    for special_key in ['unknown', 'empty']:
        if special_key in groups:
            sorted_groups.append((special_key, groups[special_key]))
    
    return sorted_groups

def generate_simple_batch_sql(street_data, rank_map, output_file="simple_batch_updates.sql", batch_size=100):
    """Generate simple batch SQL using CASE statements."""
    
    print(f"Generating Simple Batch SQL with CASE statements (batch size: {batch_size})")
    print("=" * 70)
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("-- Simple Batch SQL UPDATE using CASE statements\n")
            f.write("-- Each statement updates multiple records at once\n\n")
            
            # Create batches
            batches = []
            for i in range(0, len(street_data), batch_size):
                batch = street_data[i:i + batch_size]
                batches.append(batch)
            
            for batch_num, batch in enumerate(batches, 1):
                f.write(f"-- Batch {batch_num}\n")
                f.write("UPDATE solr_address SET\n")
                f.write("tc_stroke_seq = CASE\n")
                
                # Generate CASE for tc_stroke_seq
                for tc_street, sc_street in batch:
                    if tc_street and tc_street.strip() and tc_street != 'NULL':
                        tc_seq = rank_map.get(tc_street, 0)
                        escaped = tc_street.replace("'", "''")
                        f.write(f"    WHEN tc_street_name = '{escaped}' THEN {tc_seq}\n")
                
                f.write("    ELSE tc_stroke_seq END,\n")
                f.write("sc_stroke_seq = CASE\n")
                
                # Generate CASE for sc_stroke_seq
                for tc_street, sc_street in batch:
                    if sc_street and sc_street.strip() and sc_street != 'NULL':
                        sc_seq = rank_map.get(sc_street, 0)
                        escaped = sc_street.replace("'", "''")
                        f.write(f"    WHEN sc_street_name = '{escaped}' THEN {sc_seq}\n")
                
                f.write("    ELSE sc_stroke_seq END;\n\n")
        
        print(f"Generated {len(batches)} batch statements")
        print(f"SQL saved to: {output_file}")
        return True
        
    except Exception as e:
        print(f"Error: {e}")
        return False

def generate_batch_sql_with_temp_table(street_data, rank_map, stroke_dict, output_file="batch_stroke_updates.sql", batch_size=100):
    """Generate batch SQL using temporary table approach with first character grouping."""
    
    print(f"Generating Fast Batch SQL with first character grouping (batch size: {batch_size})")
    print("=" * 70)
    
    # Group streets by first character (for SQL organization)
    grouped_streets = group_streets_by_first_character(street_data, stroke_dict)
    
    print(f"Grouped {len(street_data)} streets into {len(grouped_streets)} first-character groups:")
    # for stroke_count, group_streets in grouped_streets:
    #     print(f"  {stroke_count} strokes: {len(group_streets)} streets")
    print()
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("-- Fast Batch SQL UPDATE with first character grouping\n")
            f.write("-- Sequence numbers are calculated based on global sort order (Strokes -> Unicode)\n\n")
            
            batch_num = 1
            
            # Process each group
            for group_stroke_count, group_streets in grouped_streets:
                f.write(f"-- === Group: First character with {group_stroke_count} strokes ({len(group_streets)} streets) ===\n")
                
                # Create batches within each group
                for i in range(0, len(group_streets), batch_size):
                    batch = group_streets[i:i + batch_size]
                    
                    f.write(f"-- Batch {batch_num} (Group {group_stroke_count}: {len(batch)} records)\n")
                    
                    # Create temporary table for this batch
                    f.write("CREATE TEMPORARY TABLE temp_stroke_updates (\n")
                    f.write("    tc_street_name TEXT,\n") 
                    f.write("    sc_street_name TEXT,\n")
                    f.write("    tc_stroke_seq INTEGER,\n")
                    f.write("    sc_stroke_seq INTEGER\n")
                    f.write(");\n\n")
                    
                    # Insert values into temp table
                    f.write("INSERT INTO temp_stroke_updates VALUES\n")
                    
                    value_rows = []
                    for tc_street, sc_street in batch:
                        # Get ranks from map
                        tc_seq = rank_map.get(tc_street, 0) if tc_street and tc_street.strip() and tc_street != 'NULL' else 0
                        sc_seq = rank_map.get(sc_street, 0) if sc_street and sc_street.strip() and sc_street != 'NULL' else 0
                        
                        # Escape strings for SQL
                        if tc_street and tc_street.strip() and tc_street != 'NULL':
                            tc_clean = tc_street.replace("'", "''").strip()
                            tc_escaped = f"'{tc_clean}'"
                        else:
                            tc_escaped = 'NULL'
                            
                        if sc_street and sc_street.strip() and sc_street != 'NULL':
                            sc_clean = sc_street.replace("'", "''").strip()
                            sc_escaped = f"'{sc_clean}'"
                        else:
                            sc_escaped = 'NULL'
                        
                        value_rows.append(f"({tc_escaped}, {sc_escaped}, {tc_seq}, {sc_seq})")
                    
                    f.write(",\n".join(value_rows))
                    f.write(";\n\n")
                    
                    # Update main table using JOIN with temp table
                    f.write("UPDATE solr_address SET\n")
                    f.write("    tc_stroke_seq = temp.tc_stroke_seq,\n")
                    f.write("    sc_stroke_seq = temp.sc_stroke_seq\n")
                    f.write("FROM temp_stroke_updates temp\n")
                    f.write("WHERE (solr_address.tc_street_name = temp.tc_street_name\n")
                    f.write("    OR solr_address.sc_street_name = temp.sc_street_name);\n\n")
                    
                    # Drop temp table
                    f.write("DROP TABLE temp_stroke_updates;\n\n")
                    
                    batch_num += 1
        
        print(f"\nGenerated {batch_num - 1} batches across {len(grouped_streets)} character groups")
        print(f"Total records: {len(street_data)}")
        print(f"SQL saved to: {output_file}")
        return True
        
    except Exception as e:
        print(f"Error generating SQL: {e}")
        return False

def main():
    # 1. Load Dictionary
    print("Loading stroke dictionary...")
    if not Path("characters.txt").exists():
        print("Error: characters.txt not found in current directory.")
        return
        
    stroke_dict = load_stroke_dict("characters.txt")
    print(f"Loaded {len(stroke_dict)} characters.")

    # 2. Connect to DB and fetch data
    # Try the path from the original script first
    db_path = "C:/DevEnv/workspace/address_sqllite/solr_address"
    
    if not Path(db_path).exists():
        # Try local directory
        if Path("solr_address").exists():
             db_path = "solr_address"
             print(f"Found database in current directory: {db_path}")
        else:
             print(f"Error: Database not found at {db_path} and not in current directory.")
             return
    else:
        print(f"Using database at: {db_path}")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("Fetching street data...")
        cursor.execute("""
            SELECT DISTINCT tc_street_name, sc_street_name 
            FROM solr_address 
            WHERE (tc_street_name IS NOT NULL AND tc_street_name != '' AND tc_street_name != 'NULL')
               OR (sc_street_name IS NOT NULL AND sc_street_name != '' AND sc_street_name != 'NULL')
        """)
        
        street_data = cursor.fetchall()
        conn.close()
        print(f"Found {len(street_data)} records.")
        
        if not street_data:
            print("No data found to process.")
            return

        # 3. Calculate Ranks
        print("Calculating ranks based on stroke count + unicode...")
        # Collect all unique names
        all_names = set()
        for tc, sc in street_data:
            if tc and tc.strip() and tc != 'NULL': all_names.add(tc)
            if sc and sc.strip() and sc != 'NULL': all_names.add(sc)
        
        # Check for missing chars
        check_missing_chars(all_names, stroke_dict)
        
        # Sort them
        sorted_names = sorted(list(all_names), key=lambda w: word_sort_key(w, stroke_dict))
        
        # Create rank map
        rank_map = {name: i+1 for i, name in enumerate(sorted_names)}
        print(f"Ranked {len(rank_map)} unique street names.")

        # 4. Generate SQL
        # generate_batch_sql_with_temp_table(street_data, rank_map, stroke_dict, "final_stroke_updates.sql")
        generate_simple_batch_sql(street_data, rank_map, "simple_batch_updates.sql")
        
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
