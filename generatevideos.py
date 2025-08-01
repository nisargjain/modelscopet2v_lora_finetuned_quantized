import os
import subprocess

def replace_and_execute_from_directory(relative_directory):
    """
    Reads all .txt files in a directory, replaces a placeholder in the command with their content, and executes the command.
    
    :param command_template: str, the command template with a placeholder {PROMPT}
    :param relative_directory: str, the relative path to the directory containing .txt files
    """
    try:
        # Calculate absolute path using os.getcwd()
        directory_path = os.path.join(os.getcwd(), relative_directory)

        # Get all .txt files in the directory
        txt_files = [f for f in os.listdir(directory_path) if f.endswith('.txt')]
        
        if not txt_files:
            print("No .txt files found in the directory.")
            return

        for file_name in txt_files:
            file_path = os.path.join(directory_path, file_name)
            try:
                with open(file_path, 'r') as file:
                    prompt = file.read().strip()  # Read and strip extra whitespace
                    # Replace the placeholder {PROMPT} in the command template
                    command=  "python inference.py --model ./outputs/train_2024-11-25T19-20-03 --prompt \"" + str(prompt) + "\" --output-dir ./test_video_inference_output_for_finetune_model --width 446 --height 336  --num-frames 80 --fps 16 --sdp"
                    # command = command_template.replace("{PROMPT}", prompt)
                    print(f"Executing command for file '{file_name}': {command}")
                    # Execute the command
                    result = subprocess.run(command, shell=True, capture_output=True, text=True)
                    # Print the output or error
                    if result.returncode == 0:
                        print(f"Success for file '{file_name}': {result.stdout}")
                    else:
                        print(f"Error for file '{file_name}': {result.stderr}")
            except Exception as e:
                print(f"Error processing file '{file_name}': {e}")
    except Exception as e:
        print(f"Error accessing directory '{relative_directory}': {e}")

# Example usage
if __name__ == "__main__":
    # Command template with a placeholder {PROMPT}
  
    # Relative path to the directory containing .txt files
    relative_directory = "test_data_for_clip"  # Replace with your relative directory path

    replace_and_execute_from_directory(relative_directory)
