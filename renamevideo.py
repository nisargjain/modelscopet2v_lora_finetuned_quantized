import os

def rename_videos(video_folder, text_folder):
    """
    Rename videos in `video_folder` to match the names of the text files in `text_folder`
    based on shared prompts.

    Args:
        video_folder (str): Path to the folder containing video files.
        text_folder (str): Path to the folder containing text files.
    """
    # Get all text files
    text_files = [f for f in os.listdir(text_folder) if f.endswith(".txt")]

    # Create a mapping of prompts to text file names
    prompt_to_textfile = {}
    for text_file in text_files:
        text_path = os.path.join(text_folder, text_file)
        with open(text_path, 'r') as file:
            prompt = file.read().strip()
            prompt_to_textfile[prompt] = text_file

    # Iterate through videos and rename them
    for video_file in os.listdir(video_folder):
        if video_file.endswith((".mp4", ".avi", ".mkv")):  # Add more video extensions if needed
            video_path = os.path.join(video_folder, video_file)

            # Check if video name matches any prompt
            for prompt, text_file in prompt_to_textfile.items():
                if prompt in video_file:
                    # Rename video to match the text file name (without .txt extension)
                    new_name = text_file.replace(".txt", os.path.splitext(video_file)[1])
                    new_path = os.path.join(video_folder, new_name)
                    os.rename(video_path, new_path)
                    print(f"Renamed: {video_file} -> {new_name}")
                    break
    print("Renaming completed.")

# Example usage
video_folder = "test_video_inference_output_for_lora_model"
text_folder = "test_data_for_clip"
rename_videos(video_folder, text_folder)
