import os
import csv
import re

def extract_number_from_filename(filename):
    """
    Extracts the numeric part from a filename like 'video123.txt'.

    Args:
        filename (str): The filename to extract the number from.

    Returns:
        int: The extracted number, or None if no number is found.
    """
    match = re.search(r'\d+', filename)
    return int(match.group()) if match else None

def generate_csv_from_txt(folder_path, output_csv):
    """
    Generates a CSV file containing numbers (extracted from filenames) and prompts from .txt files in a folder.
    
    Args:
        folder_path (str): Path to the folder containing .txt files.
        output_csv (str): Path to save the generated CSV file.
    """
    txt_files = [f for f in os.listdir(folder_path) if f.endswith('.txt')]
    txt_files.sort()  # Sort files for consistent order

    data = []
    for txt_file in txt_files:
        file_path = os.path.join(folder_path, txt_file)
        number = extract_number_from_filename(txt_file)
        if number is not None:
            with open(file_path, 'r') as file:
                prompt = file.read().strip()  # Read and strip any extra whitespace
            data.append({"Number": number, "Prompt": prompt})
        else:
            print(f"Warning: No number found in filename {txt_file}")

    # Write to CSV
    with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["Number", "Prompt"])
        writer.writeheader()
        writer.writerows(data)

    print(f"CSV file saved at: {output_csv}")

# Example usage
folder_path = os.path.join(os.getcwd(), "test_data_for_clip")  # Replace 'text_files' with your folder name
output_csv = "prompts_with_numbers.csv"
generate_csv_from_txt(folder_path, output_csv)
