import os

def rename_videos_to_prompts(folder_path):
    """
    Rename video files to the corresponding prompts in their associated text files.

    Args:
        folder_path (str): Path to the folder containing the video and text files.
    """
    for file in os.listdir(folder_path):
        # Check if the file is a text file
        if file.endswith(".txt"):
            # Get the video filename
            base_name = file.replace(".txt", "")
            video_file = os.path.join(folder_path, f"{base_name}.mp4")
            text_file = os.path.join(folder_path, file)

            # Check if the corresponding video file exists
            if os.path.exists(video_file):
                # Read the prompt from the text file
                with open(text_file, 'r') as txt:
                    prompt = txt.read().strip()

                # Construct the new video filename
                # Ensure prompt is sanitized for filename compatibility
                sanitized_prompt = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
                new_video_name = f"{sanitized_prompt}.mp4"
                new_video_path = os.path.join(folder_path, new_video_name)

                # Rename the video file
                try:
                    os.rename(video_file, new_video_path)
                    print(f"Renamed: {video_file} -> {new_video_path}")
                except Exception as e:
                    print(f"Error renaming {video_file}: {e}")
            else:
                print(f"Video file not found for text file: {file}")

# Replace with the path to your folder
folder_path = "./test_video_inference_output_for_lora_model"
rename_videos_to_prompts(folder_path)
