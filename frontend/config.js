// Local test: your backend port. After deploy: the Lambda Function URL (no trailing slash).
// Teacher name, subject and voice come from backend/.env automatically.
window.TWIN_CONFIG = {
  apiBase: "http://localhost:8009",

  // Avatar: "photo" = her photo with lip-sync (media/face.jpg), "video" = her talking/idle video loops
  avatarMode: "photo",
  facePhoto: "media/face.jpg",
  // Mouth position in the photo (fractions of width/height). Used only if automatic face detection fails.
  mouth: { x: 0.488, y: 0.451, w: 0.123, chin: 0.559 },
  mouthStrength: 1.0,   // 0.6 = subtler, 1.3 = bigger mouth movement
  avatarDebug: false,   // true = show green marks where it found her mouth and chin
};
