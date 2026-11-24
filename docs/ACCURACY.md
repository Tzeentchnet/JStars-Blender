# Accuracy checks done

- Bone chains rebuilt from local matrices match the files' inverse-bind matrices (max error 1.7e-7).
- Skinned meshes in rest pose reproduce the raw vertex data exactly.
- Imported motions were checked against an independent NumPy forward-kinematics pass: Blender's evaluated bone positions match at every sampled frame.
- Every stage (25 variants), every battle and J-Adventure model (122) and a sample of world-map models import without errors in background mode.
