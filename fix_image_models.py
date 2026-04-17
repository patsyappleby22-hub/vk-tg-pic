with open('vk_bot/keyboards.py', 'r') as f:
    content = f.read()

content = content.replace("""    for model_id, info in image_models.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        # We can just use the short name to save space if needed, but let's keep it.
        # Actually, VK allows up to 5 buttons per row, but total text length might be an issue.
        # Let's put 2 images per row.
        """, "")

with open('vk_bot/keyboards.py', 'w') as f:
    f.write(content)
print("Done fixing keyboard duplicate loop")
