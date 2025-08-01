import os
import cv2
import clip
import torch
from PIL import Image
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
import numpy as np


# Load CLIP model and preprocess function
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)

def read_prompt_from_file(prompt_file):
    """Reads the prompt from a text file."""
    with open(prompt_file, 'r') as file:
        return file.read().strip()

def calculate_clip_score(video_path, prompt):
    """
    Calculate the average CLIP score for a video based on a given prompt.

    Args:
        video_path (str): Path to the video file.
        prompt (str): Original prompt to compare frames against.

    Returns:
        float: Average CLIP score for the video.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Failed to open video: {video_path}")
        return None

    fps = int(cap.get(cv2.CAP_PROP_FPS))  # Get frames per second
    frame_interval = max(fps // 6, 1)  # Ensure we sample at least 1 frame per second if fps < 6

    frame_scores = []
    frame_count = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_count % frame_interval == 0:
            # Convert the frame to PIL Image and preprocess it for CLIP
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_frame = Image.fromarray(frame_rgb)
            input_image = preprocess(pil_frame).unsqueeze(0).to(device)

            # Encode prompt and image
            with torch.no_grad():
                text_features = model.encode_text(clip.tokenize([prompt]).to(device))
                image_features = model.encode_image(input_image)

                # Normalize features and calculate cosine similarity
                text_features /= text_features.norm(dim=-1, keepdim=True)
                image_features /= image_features.norm(dim=-1, keepdim=True)
                similarity = (text_features @ image_features.T).item()

                frame_scores.append(similarity)

        frame_count += 1

    cap.release()

    if frame_scores:
        return sum(frame_scores) / len(frame_scores)
    else:
        print(f"No frames processed for video: {video_path}")
        return None

def main(folder_path):
    """
    Process all video and prompt pairs in a folder and calculate average CLIP score.

    Args:
        folder_path (str): Path to the folder containing videos and prompts.
    """
    ll = []
    for file in os.listdir(folder_path):
        if file.endswith(".mp4"):
            video_file = os.path.join(folder_path, file)
            prompt_file = os.path.join(folder_path, file.replace(".mp4", ".txt"))

            if not os.path.exists(prompt_file):
                print(f"No matching prompt file found for video: {file}")
                continue

            prompt = read_prompt_from_file(prompt_file)
            # print(f"Processing video: {file} with prompt: '{prompt}'")

            avg_score = calculate_clip_score(video_file, prompt)
            ll.append(avg_score)
            # if avg_score is not None:
            #     # print(f"Average CLIP score for {file}: {avg_score:.4f}")
            # else:
            #     print(f"Could not calculate CLIP score for {file}")
    avg_score = np.mean(np.array(ll))
    print(f"Average CLIP score for Test Data: {avg_score:.4f}")


if __name__ == "__main__":
    folder_path = os.path.join(os.getcwd(), "test_video_inference_output_for_lora_model")  # Adjust relative path if needed
    main(folder_path)
