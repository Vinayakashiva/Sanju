// web/app.js
//
// Everything the avatar page does: load the VRM model, load and retarget
// the Mixamo animation clips, run the idle/breathing/blink/head-sway
// procedural motion, drive lip sync from Python-classified visemes, and
// run a small animation state machine (idle loops forever; every other
// clip plays once and returns to idle automatically, acknowledged back to
// Python over QWebChannel).

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';

// ════════════════════════════════════════════════════════════════════════
// Mixamo → VRM bone retargeting
// ════════════════════════════════════════════════════════════════════════
// animations/*.fbx are assumed to be Mixamo-exported clips (the standard
// source for this kind of humanoid motion capture) — their bone names use
// the "mixamorig" prefix, which doesn't match VRM's standardized humanoid
// bone names, so every track has to be remapped onto the VRM's actual
// bone nodes before it can play on this model.
const MIXAMO_VRM_BONE_MAP = {
  mixamorigHips: 'hips', mixamorigSpine: 'spine', mixamorigSpine1: 'chest',
  mixamorigSpine2: 'upperChest', mixamorigNeck: 'neck', mixamorigHead: 'head',
  mixamorigLeftShoulder: 'leftShoulder', mixamorigLeftArm: 'leftUpperArm',
  mixamorigLeftForeArm: 'leftLowerArm', mixamorigLeftHand: 'leftHand',
  mixamorigRightShoulder: 'rightShoulder', mixamorigRightArm: 'rightUpperArm',
  mixamorigRightForeArm: 'rightLowerArm', mixamorigRightHand: 'rightHand',
  mixamorigLeftUpLeg: 'leftUpperLeg', mixamorigLeftLeg: 'leftLowerLeg',
  mixamorigLeftFoot: 'leftFoot', mixamorigLeftToeBase: 'leftToes',
  mixamorigRightUpLeg: 'rightUpperLeg', mixamorigRightLeg: 'rightLowerLeg',
  mixamorigRightFoot: 'rightFoot', mixamorigRightToeBase: 'rightToes',
};

function retargetMixamoClip(fbxAsset, vrm) {
  const clip =
    THREE.AnimationClip.findByName(fbxAsset.animations, 'mixamo.com') ||
    fbxAsset.animations[0];
  if (!clip) throw new Error('[Mixamo] No animation clip found in FBX asset.');

  const tracks = [];
  const isVRM0 = vrm.meta?.metaVersion === '0';
  const restRotationInverse = new THREE.Quaternion();
  const parentRestWorldRotation = new THREE.Quaternion();
  const quatWork = new THREE.Quaternion();
  const vecWork = new THREE.Vector3();

  const mixamoHips = fbxAsset.getObjectByName('mixamorigHips');
  const mixamoHipsHeight = mixamoHips ? mixamoHips.position.y : 100;
  const vrmHipsY = vrm.humanoid.getNormalizedBoneNode('hips').getWorldPosition(vecWork).y;
  const vrmRootY = vrm.scene.getWorldPosition(vecWork).y;
  const vrmHipsHeight = Math.abs(vrmHipsY - vrmRootY) || 1;
  const hipsPositionScale = vrmHipsHeight / mixamoHipsHeight;

  clip.tracks.forEach((track) => {
    const [mixamoRigName, propertyName] = track.name.split('.');
    const vrmBoneName = MIXAMO_VRM_BONE_MAP[mixamoRigName];
    if (!vrmBoneName) return;

    const vrmBoneNode = vrm.humanoid.getNormalizedBoneNode(vrmBoneName);
    const mixamoRigNode = fbxAsset.getObjectByName(mixamoRigName);
    if (!vrmBoneNode || !mixamoRigNode) return;

    const vrmNodeName = vrmBoneNode.name;

    if (track instanceof THREE.QuaternionKeyframeTrack) {
      mixamoRigNode.getWorldQuaternion(restRotationInverse).invert();
      mixamoRigNode.parent.getWorldQuaternion(parentRestWorldRotation);
      const values = track.values.slice();
      for (let i = 0; i < values.length; i += 4) {
        quatWork.fromArray(values, i);
        quatWork.premultiply(parentRestWorldRotation).multiply(restRotationInverse);
        if (isVRM0) { quatWork.x *= -1; quatWork.z *= -1; }
        quatWork.toArray(values, i);
      }
      tracks.push(new THREE.QuaternionKeyframeTrack(`${vrmNodeName}.${propertyName}`, track.times, values));
    } else if (track instanceof THREE.VectorKeyframeTrack) {
      const values = track.values.map((v, i) => {
        const scaled = v * hipsPositionScale;
        if (isVRM0 && i % 3 !== 1) return -scaled;
        return scaled;
      });
      tracks.push(new THREE.VectorKeyframeTrack(`${vrmNodeName}.${propertyName}`, track.times, values));
    }
  });

  return new THREE.AnimationClip(clip.name || 'mixamoClip', clip.duration, tracks);
}

// The 5 standard VRM lip-sync viseme presets (required by the VRM spec on
// any humanoid model that supports lip sync at all). Python (tts.py)
// classifies Kokoro's own phoneme output into these 5 groups and streams
// the result over via setViseme(); this file only has to blend toward it.
const VISEME_NAMES = ['aa', 'ih', 'ee', 'oh', 'ou'];
const IDLE_NAME = 'idle';

// A pulled-back, angled "runway" view used while the catwalk clip plays,
// then tweened back to the default view once she returns to idle.
const CATWALK_CAMERA = {
  pos: new THREE.Vector3(1.35, 1.25, 2.0),
  look: new THREE.Vector3(0, 1.1, 0),
};

// A pulled-back, lowered view used while she's sitting and typing — the
// default portrait framing crops out her hands entirely once she's seated
// (they're well below the tight bust-level default crop).
const TYPE_CAMERA = {
  pos: new THREE.Vector3(0, 1.05, 2.6),
  look: new THREE.Vector3(0, 0.8, 0),
  fov: 32,
};

// A pulled-back, lowered view used while she's walking to the other side
// of the screen (see walkAndTurn) — the default portrait framing is tight
// enough that her legs/feet are cropped out, which makes a walk cycle
// unreadable. Wider FOV + further back + lower look-target brings her
// whole body into frame without losing her from the shot.
const WALK_CAMERA = {
  pos: new THREE.Vector3(0, 1.15, 3.6),
  look: new THREE.Vector3(0, 0.95, 0),
  fov: 38,
};

// How much she turns to face her direction of travel before walking, and
// how long that turn (and the turn back to the camera afterward) takes.
// 75° reads clearly as "she's heading that way" while staying well short
// of 90° (pure profile) or 180° (which would show her back — never do
// that; see walkAndTurn).
const WALK_TURN_ANGLE = Math.PI * (75 / 180);
const WALK_TURN_DURATION = 0.45; // seconds

// Sign of the Y-rotation that turns her to face screen-left vs
// screen-right, given this scene's camera (parked on +Z, looking back
// toward -Z) and the orientation VRMUtils.rotateVRM0 leaves the model in.
// If left/right ever come out visually swapped for a different VRM
// export, flip these two signs — nothing else needs to change.
const WALK_DIRECTION_SIGN = { left: -1, right: 1 };

export class VRMAvatarController {
  constructor(canvas) {
    this.canvas = canvas;
    this.vrm = null;
    this.mixer = null;
    this._actions = {};          // name -> THREE.AnimationAction
    this._currentActionName = null;

    this._talking = false;
    this._activeViseme = null;
    this._visemeWeight = 0;
    this._visemeCurrent = {};

    this._nextBlinkAt = this._randomBlinkDelay();
    this._blinkPhase = 0;
    this._headSwaySeed = Math.random() * 1000;

    // Camera tweening — used to swing to a "runway" angle during the
    // catwalk animation, to a wider full-body angle while she's walking
    // (see WALK_CAMERA / walkAndTurn), and back to the default view
    // afterward in both cases.
    this._cameraDefault = {
      pos: new THREE.Vector3(0, 1.35, 2.4),
      look: new THREE.Vector3(0, 1.2, 0),
      fov: 28,
    };
    this._cameraFrom = this._cameraDefault.pos.clone();
    this._cameraFromLook = this._cameraDefault.look.clone();
    this._cameraTo = this._cameraDefault.pos.clone();
    this._cameraToLook = this._cameraDefault.look.clone();
    this._cameraLookCurrent = this._cameraDefault.look.clone();
    this._cameraFovFrom = this._cameraDefault.fov;
    this._cameraFovTo = this._cameraDefault.fov;
    this._cameraFovCurrent = this._cameraDefault.fov;
    this._cameraTweenStart = 0;
    this._cameraTweenDuration = 0;

    // Rotation tweening — turns the model to face her direction of travel
    // before/after a walkAndTurn move, kept separate from the model's
    // initial VRM0 orientation fix (VRMUtils.rotateVRM0, applied once in
    // loadVRM) so the two never fight each other.
    this._rotationFrom = 0;
    this._rotationTo = 0;
    this._rotationCurrent = 0;
    this._rotationTweenStart = 0;
    this._rotationTweenDuration = 0;
    this._baseRotationY = 0; // set for real once loadVRM runs rotateVRM0

    // Timer handles for playAnimationTimed / walkAndTurn (looped clips for
    // a fixed duration, used while the desktop window itself is sliding).
    this._timedAnimHandle = null;
    this._turnBackHandle = null;

    this._initScene();
    this._initLights();

    this._animate = this._animate.bind(this);
    requestAnimationFrame(this._animate);
    window.addEventListener('resize', () => this._onResize());

    // Recover from a lost WebGL context (window hidden/minimized, GPU
    // process restart, etc.) instead of leaving a permanently frozen
    // frame — a common cause of an avatar that "stops displaying".
    this.canvas.addEventListener('webglcontextlost', (e) => {
      e.preventDefault();
      console.warn('[VRM] WebGL context lost — will reinitialize on restore.');
    });
    this.canvas.addEventListener('webglcontextrestored', () => {
      console.warn('[VRM] WebGL context restored — reinitializing scene.');
      const vrmScene = this.vrm?.scene;
      this._initScene();
      this._initLights();
      if (vrmScene) this.scene.add(vrmScene);
      this._onResize();
    });
  }

  _initScene() {
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, alpha: true, antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(28, 1, 0.1, 20);
    this.camera.position.set(0, 1.35, 2.4);
    this.camera.lookAt(0, 1.2, 0);

    this.clock = new THREE.Clock();
    this._onResize();
  }

  _initLights() {
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(1, 2, 2);
    const fill = new THREE.AmbientLight(0xffffff, 0.6);
    this.scene.add(key, fill);
  }

  _onResize() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (!w || !h) return; // avoid a 0x0 renderer.setSize(), which can wedge the WebGL context
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  // ── Loading ──────────────────────────────────────────────────────────
  async loadVRM(url) {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));

    const gltf = await loader.loadAsync(url);
    const vrm = gltf.userData.vrm;
    if (!vrm) throw new Error('[VRM] Loaded file has no VRM extension data — is this really a .vrm file?');

    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.removeUnnecessaryJoints(gltf.scene);
    // VRM0-spec models (the vast majority exported from VRoid/Booth) face
    // +Z instead of the -Z convention three.js/glTF expect, so without
    // this they render facing away from the camera. Safe to call
    // unconditionally — it's a no-op on VRM1 models.
    VRMUtils.rotateVRM0(vrm);
    vrm.scene.traverse((obj) => { obj.frustumCulled = false; });

    // rotateVRM0 above is what makes a VRM0-spec model face the camera in
    // the first place (it applies a base Y-rotation, typically 180°, to
    // vrm.scene). Remember that value so walkAndTurn's rotation tween can
    // ADD its turn on top of it instead of overwriting it — see
    // _updateRotationTween, which would otherwise snap her to face the
    // wrong way the instant a turn starts.
    this._baseRotationY = vrm.scene.rotation.y;

    if (this.vrm) this.scene.remove(this.vrm.scene);
    this.vrm = vrm;
    this.scene.add(vrm.scene);
    this.mixer = new THREE.AnimationMixer(vrm.scene);
    this._actions = {};
    this._currentActionName = null;
    return vrm;
  }

  async loadAnimationClip(name, path) {
    if (!this.vrm || !this.mixer) {
      throw new Error(`[VRM] Cannot load clip '${name}' before a VRM model is loaded.`);
    }
    const loader = new FBXLoader();
    const asset = await loader.loadAsync(path);
    const clip = retargetMixamoClip(asset, this.vrm);
    const action = this.mixer.clipAction(clip);
    action.enabled = true;
    this._actions[name] = action;
    return action;
  }

  hasAction(name) {
    return !!this._actions[name];
  }

  // ── Animation state machine ─────────────────────────────────────────
  // idle loops forever. Every other clip plays exactly once and then
  // automatically returns to idle — driven off the mixer's own 'finished'
  // event (real playback completion), not a guessed timeout, so nothing
  // can get stuck mid-animation.
  startIdle() {
    this._crossFadeTo(IDLE_NAME, { loop: true });
  }

  playAnimation(name) {
    if (!this.hasAction(name)) {
      console.warn(`[VRM] playAnimation('${name}') requested but that clip isn't loaded.`);
      return;
    }
    this._cancelWalkTurn();
    if (name === IDLE_NAME) {
      this.startIdle();
      return;
    }
    this._handleCatwalkCameraTransition(name);
    this._crossFadeTo(name, { loop: false });
  }

  // Like playAnimation, but loops indefinitely instead of playing once —
  // for held poses that last as long as a state does (e.g. 'wait' while
  // LISTENING, 'sitting_idle' while asleep), not a one-shot reaction.
  playAnimationLoop(name) {
    if (!this.hasAction(name)) {
      console.warn(`[VRM] playAnimationLoop('${name}') requested but that clip isn't loaded.`);
      return;
    }
    if (name === IDLE_NAME) {
      this.startIdle();
      return;
    }
    this._cancelWalkTurn();
    this._handleCatwalkCameraTransition(name);
    this._crossFadeTo(name, { loop: true });
  }

  // Loops a clip for a fixed duration instead of playing it once, then
  // returns to idle automatically — used for "walking" while the desktop
  // window itself slides to a new position (see slide_to_side in
  // vrm_bridge.py / _handle_move in assistant.py), so the clip's own
  // natural length doesn't need to match the slide's duration.
  playAnimationTimed(name, durationSeconds) {
    if (!this.hasAction(name)) {
      console.warn(`[VRM] playAnimationTimed('${name}') requested but that clip isn't loaded.`);
      return;
    }
    this._cancelWalkTurn();
    this._handleCatwalkCameraTransition(name);
    this._crossFadeTo(name, { loop: true });

    if (this._timedAnimHandle) clearTimeout(this._timedAnimHandle);
    this._timedAnimHandle = setTimeout(() => {
      this._timedAnimHandle = null;
      if (this._currentActionName !== name) return; // superseded already
      this._notifyAnimationFinished(name);
      this._handleCatwalkCameraTransition(IDLE_NAME);
      this.startIdle();
    }, Math.max(0, durationSeconds) * 1000);
  }

  // Turns her to face the direction of travel, loops the given walk clip
  // (walk / walk2 / lazy — assistant.py picks one at random each time)
  // while the desktop window slides (see slide_to_side in vrm_bridge.py /
  // _handle_move in assistant.py), then turns back to face the camera and
  // returns to idle. Never rotates a full 180° — she's turned toward
  // 'left' or 'right' by WALK_TURN_ANGLE, never enough to show her back.
  walkAndTurn(direction, durationSeconds, clipName = 'walk') {
    if (!this.hasAction(clipName)) {
      console.warn(`[VRM] walkAndTurn requested but the '${clipName}' clip isn't loaded.`);
      return;
    }
    this._cancelWalkTurn();

    const sign = WALK_DIRECTION_SIGN[direction] ?? 1;
    const targetY = WALK_TURN_ANGLE * sign;
    // Guarantee room for a turn out and a turn back even if a very short
    // duration were ever passed in.
    const total = Math.max(WALK_TURN_DURATION * 2 + 0.2, durationSeconds);

    // Turn toward the destination and widen the camera at the same time —
    // she starts turning immediately rather than pausing dead still first.
    this._tweenRotationTo(targetY, WALK_TURN_DURATION);
    this._tweenCameraTo(WALK_CAMERA.pos, WALK_CAMERA.look, WALK_TURN_DURATION, WALK_CAMERA.fov);
    this._crossFadeTo(clipName, { loop: true });

    // Turn back to face the camera and restore the normal framing shortly
    // before the slide finishes, so she's fully facing forward again right
    // as she settles at the destination.
    const turnBackAt = Math.max(0, total - WALK_TURN_DURATION) * 1000;
    this._turnBackHandle = setTimeout(() => {
      this._turnBackHandle = null;
      this._tweenRotationTo(0, WALK_TURN_DURATION);
      this._tweenCameraTo(this._cameraDefault.pos, this._cameraDefault.look, WALK_TURN_DURATION, this._cameraDefault.fov);
    }, turnBackAt);

    this._timedAnimHandle = setTimeout(() => {
      this._timedAnimHandle = null;
      if (this._currentActionName !== clipName) return; // superseded already
      this._notifyAnimationFinished(clipName);
      this.startIdle();
    }, total * 1000);
  }

  // Clears any pending walkAndTurn timers and, if she's mid-turn, tweens
  // her back to facing the camera — called whenever a different command
  // interrupts a walkAndTurn in progress, so she never gets stuck turned
  // sideways.
  _cancelWalkTurn() {
    if (this._timedAnimHandle) { clearTimeout(this._timedAnimHandle); this._timedAnimHandle = null; }
    if (this._turnBackHandle) { clearTimeout(this._turnBackHandle); this._turnBackHandle = null; }
    if (this._rotationCurrent !== 0 || this._rotationTweenDuration > 0) {
      this._tweenRotationTo(0, WALK_TURN_DURATION);
    }
  }

  // Swings the camera to a special framing when entering 'catwalk' (runway
  // angle) or 'sit_to_type'/'typing' (pulled back so her hands are in
  // shot), and back to the default view when leaving either for anything
  // else.
  _handleCatwalkCameraTransition(nextName) {
    const specialFraming = (name) => {
      if (name === 'catwalk') return CATWALK_CAMERA;
      if (name === 'sit_to_type' || name === 'typing') return TYPE_CAMERA;
      return null;
    };
    const prevFraming = specialFraming(this._currentActionName);
    const nextFraming = specialFraming(nextName);
    if (nextFraming && nextFraming !== prevFraming) {
      this._tweenCameraTo(nextFraming.pos, nextFraming.look, 0.7);
    } else if (!nextFraming && prevFraming) {
      this._tweenCameraTo(this._cameraDefault.pos, this._cameraDefault.look, 0.7);
    }
  }

  _tweenCameraTo(pos, look, duration = 0.7, fov = this._cameraDefault.fov) {
    this._cameraFrom.copy(this.camera.position);
    this._cameraFromLook.copy(this._cameraLookCurrent);
    this._cameraTo.copy(pos);
    this._cameraToLook.copy(look);
    this._cameraFovFrom = this._cameraFovCurrent;
    this._cameraFovTo = fov;
    this._cameraTweenStart = this.clock.elapsedTime;
    this._cameraTweenDuration = Math.max(0.001, duration);
  }

  _updateCameraTween() {
    if (this._cameraTweenDuration <= 0) return;
    const raw = (this.clock.elapsedTime - this._cameraTweenStart) / this._cameraTweenDuration;
    const t = Math.min(1, Math.max(0, raw));
    const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
    this.camera.position.lerpVectors(this._cameraFrom, this._cameraTo, eased);
    this._cameraLookCurrent.lerpVectors(this._cameraFromLook, this._cameraToLook, eased);
    this.camera.lookAt(this._cameraLookCurrent);
    this._cameraFovCurrent = this._cameraFovFrom + (this._cameraFovTo - this._cameraFovFrom) * eased;
    this.camera.fov = this._cameraFovCurrent;
    this.camera.updateProjectionMatrix();
    if (t >= 1) this._cameraTweenDuration = 0;
  }

  // Turns the model to face her direction of travel (walkAndTurn) without
  // touching the model's initial VRM0 orientation fix (VRMUtils.rotateVRM0
  // in loadVRM, applied once at load time) — this rotates vrm.scene from
  // whatever that fix left it at, purely as an offset for walking.
  _tweenRotationTo(targetY, duration = WALK_TURN_DURATION) {
    this._rotationFrom = this._rotationCurrent;
    this._rotationTo = targetY;
    this._rotationTweenStart = this.clock.elapsedTime;
    this._rotationTweenDuration = Math.max(0.001, duration);
  }

  _updateRotationTween() {
    if (this._rotationTweenDuration <= 0) return;
    const raw = (this.clock.elapsedTime - this._rotationTweenStart) / this._rotationTweenDuration;
    const t = Math.min(1, Math.max(0, raw));
    const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
    this._rotationCurrent = this._rotationFrom + (this._rotationTo - this._rotationFrom) * eased;
    if (this.vrm) this.vrm.scene.rotation.y = this._baseRotationY + this._rotationCurrent;
    if (t >= 1) this._rotationTweenDuration = 0;
  }

  _crossFadeTo(name, { loop }) {
    const next = this._actions[name];
    if (!next) return;

    // Re-triggering the SAME clip that's already playing/mid-fade-in
    // (e.g. repeated set_state("LISTENING") calls firing playAnimationLoop
    // ('wait') again before the first fade finished) used to call
    // next.reset()+fadeIn() on it anyway, snapping its weight back to 0
    // for a frame with nothing else weighted to cover the gap — a visible
    // flash to the raw bind/T-pose. Since it's already the target, there's
    // nothing to do.
    if (this._currentActionName === name && next.enabled) {
      return;
    }

    const prevName = this._currentActionName;
    const prev = prevName ? this._actions[prevName] : null;

    next.reset();
    if (loop) {
      next.setLoop(THREE.LoopRepeat, Infinity);
      next.clampWhenFinished = false;
    } else {
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
    }
    next.enabled = true;
    next.fadeIn(0.3);
    next.play();

    if (prev && prev !== next) {
      prev.fadeOut(0.3);
    }

    this._currentActionName = name;

    if (!loop) {
      const onFinished = (e) => {
        if (e.action !== next) return;
        this.mixer.removeEventListener('finished', onFinished);
        // If a newer command superseded this one before it finished, don't
        // stomp on whatever's playing now.
        if (this._currentActionName !== name) return;
        this._notifyAnimationFinished(name);
        if (name === 'catwalk') {
          this._tweenCameraTo(this._cameraDefault.pos, this._cameraDefault.look, 0.7);
        }
        this.startIdle();
      };
      this.mixer.addEventListener('finished', onFinished);
    }
  }

  _notifyAnimationFinished(name) {
    // Acknowledge completion back to Python over QWebChannel (see
    // web/index.html for the qwebchannel.js include and vrm_bridge.py for
    // the AssistantBridge that receives this call).
    if (window.pyBridge && typeof window.pyBridge.animationFinished === 'function') {
      window.pyBridge.animationFinished(name);
    }
  }

  // ── Talking / lip sync ──────────────────────────────────────────────
  // Speech is shown through lip-sync (setViseme/setMouthLevel) only —
  // deliberately no dedicated "talking" body-animation loop, so whatever
  // pose she's already holding (idle, wait, ...) just keeps playing
  // underneath the mouth movement.
  setTalking(isTalking) {
    this._talking = isTalking;
    if (!isTalking) {
      this._activeViseme = null;
      this._visemeWeight = 0;
    }
  }

  setMouthLevel(volume0to100) {
    // This app's TTS pipeline (Kokoro via audio_engine.py) never calls
    // setViseme with real phoneme data — dynamic_pulse -> setMouthLevel is
    // the ONLY signal driving her mouth here. _updateLipSync below uses it
    // as a plain amplitude-driven mouth-open ('aa') fallback whenever no
    // real viseme is active.
    this._lastVolume = Math.max(0, Math.min(1, volume0to100 / 100));
  }

  setViseme(name, weight) {
    this._activeViseme = name || null;
    this._visemeWeight = Math.max(0, Math.min(1, weight));
  }

  _hasExpression(name) {
    return !!this.vrm?.expressionManager?.getExpression(name);
  }

  _updateLipSync(dt) {
    if (!this.vrm?.expressionManager) return;
    const em = this.vrm.expressionManager;
    const lerpSpeed = Math.min(1, dt * 18);
    // Raw volume alone reads as a barely-visible twitch — boost it so
    // normal speaking volume actually reaches a clearly open mouth.
    const MOUTH_OPEN_GAIN = 1.8;
    VISEME_NAMES.forEach((v) => {
      let target = (v === this._activeViseme) ? this._visemeWeight : 0;
      if (!this._activeViseme && v === 'aa') {
        target = Math.min(1, (this._lastVolume || 0) * MOUTH_OPEN_GAIN);
      }
      const current = this._visemeCurrent[v] || 0;
      const next = current + (target - current) * lerpSpeed;
      this._visemeCurrent[v] = next;
      if (this._hasExpression(v)) em.setValue(v, next);
    });
  }

  // ── Idle procedural motion: blinking, breathing, head sway ─────────
  _updateBlink(t) {
    if (!this._hasExpression('blink')) return;
    if (t >= this._nextBlinkAt && this._blinkPhase === 0) {
      this._blinkPhase = 0.0001;
    }
    if (this._blinkPhase > 0) {
      this._blinkPhase += 0.09;
      const v = Math.sin(Math.min(this._blinkPhase, 1) * Math.PI);
      this.vrm.expressionManager.setValue('blink', Math.max(0, v));
      if (this._blinkPhase >= 1) {
        this._blinkPhase = 0;
        this._nextBlinkAt = t + this._randomBlinkDelay();
      }
    }
  }

  _randomBlinkDelay() {
    return 2 + Math.random() * 4; // seconds between blinks
  }

  _updateBreathing(t) {
    const chest = this.vrm?.humanoid?.getNormalizedBoneNode('chest')
      || this.vrm?.humanoid?.getNormalizedBoneNode('spine');
    if (!chest) return;
    chest.rotation.x = Math.sin(t * 0.9) * 0.02;
  }

  _updateHeadSway(t) {
    const head = this.vrm?.humanoid?.getNormalizedBoneNode('head');
    if (!head) return;
    const seed = this._headSwaySeed;
    head.rotation.y = Math.sin(t * 0.35 + seed) * 0.06;
    head.rotation.x = Math.sin(t * 0.5 + seed * 1.3) * 0.03;
  }

  // ── Render loop ──────────────────────────────────────────────────
  _animate() {
    requestAnimationFrame(this._animate);
    // Guard against rendering into a 0x0 or not-yet-attached canvas — a
    // real render() call in that state can wedge the WebGL context rather
    // than just producing a blank frame.
    if (!this.canvas.clientWidth || !this.canvas.clientHeight) return;

    const dt = Math.min(this.clock.getDelta(), 0.1);
    const t = this.clock.elapsedTime;

    if (this.mixer) this.mixer.update(dt);

    if (this.vrm) {
      this._updateBlink(t);
      this._updateBreathing(t);
      if (!this._talking) this._updateHeadSway(t);
      if (this._talking) this._updateLipSync(dt);
      this.vrm.update(dt);
    }

    this._updateCameraTween();
    this._updateRotationTween();
    this.renderer.render(this.scene, this.camera);
  }
}

// ════════════════════════════════════════════════════════════════════════
// Bootstrap — runs as soon as this module loads (index.html's only script)
// ════════════════════════════════════════════════════════════════════════

// Readiness flags polled by vrm_bridge.py so Python never calls into a page
// (or a specific clip) before it's actually ready to receive that call.
window.__controllerReady = false;
window.__animsReady = false;
window.__lastError = null;

window.addEventListener('error', (e) => {
  window.__lastError = String(e.message || e);
  console.error('[VRM] Uncaught error:', e.error || e.message);
});

// QWebChannel — connects window.pyBridge once Qt's transport is available.
// This is Qt WebEngine's built-in in-process bridge, not a websocket and
// not a localhost server (see index.html and vrm_bridge.py).
window.pyBridge = null;
if (window.qt && window.qt.webChannelTransport) {
  new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
    window.pyBridge = channel.objects.pyBridge;
    console.log('[web] QWebChannel connected.');
  });
} else {
  console.warn('[web] QWebChannel transport not found — animation-finished acknowledgements will be skipped.');
}

const canvas = document.getElementById('avatar-canvas');
const avatar = new VRMAvatarController(canvas);
window.avatar = avatar;

window.playAnimation = (name) => avatar.playAnimation(name);
window.playAnimationLoop = (name) => avatar.playAnimationLoop(name);
window.playAnimationTimed = (name, durationSeconds) => avatar.playAnimationTimed(name, durationSeconds);
window.walkAndTurn = (direction, durationSeconds, clipName) => avatar.walkAndTurn(direction, durationSeconds, clipName);
window.setTalking    = (v)    => avatar.setTalking(v);
window.setMouthLevel = (v)    => avatar.setMouthLevel(v);
window.setViseme     = (name, weight) => avatar.setViseme(name, weight);

window.__controllerReady = true;

// On first paint inside a freshly-created QWebEngineView the canvas can
// briefly report a 0x0 client size; re-asserting a resize shortly after
// load prevents that from ever wedging the renderer.
function kickResize() { window.dispatchEvent(new Event('resize')); }
requestAnimationFrame(kickResize);
setTimeout(kickResize, 250);
setTimeout(kickResize, 1000);

(async () => {
  try {
    await avatar.loadVRM('../models/sanju.vrm');
    console.log('[VRM] Model loaded.');

    const clips = [
      ['idle', '../animations/Idle.fbx'],
      ['wait', '../animations/wait.fbx'],
      ['walk', '../animations/Walk.fbx'],
      ['walk2', '../animations/Walk2.fbx'],
      ['lazy', '../animations/lazy.fbx'],
      ['catwalk', '../animations/Catwalk Walk.fbx'],
      ['cat_walk', '../animations/cat_walk.fbx'],
      ['clap', '../animations/clap.fbx'],
      ['sit', '../animations/sit.fbx'],
      ['sitting_idle', '../animations/Sitting Idle.fbx'],
      ['happy', '../animations/Happy.fbx'],
      // All 7 dance clips now live in animations/dance/ (a subfolder),
      // not directly in animations/ — update this path again if you move
      // them elsewhere.
      ['dance', '../animations/dance/dance.fbx'],
      ['hiphop', '../animations/dance/Hip Hop Dancing.fbx'],
      ['breakdance', '../animations/dance/Breakdance 1990.fbx'],
      ['dancing1', '../animations/dance/Dancing (1).fbx'],
      ['dancing_maraschino', '../animations/dance/Dancing Maraschino Step.fbx'],
      ['snake_hiphop', '../animations/dance/Snake Hip Hop Dance.fbx'],
      ['happy_dance', '../animations/dance/happy_dance.fbx'],
      ['waving', '../animations/Waving.fbx'],
      ['standing_greeting', '../animations/Standing Greeting.fbx'],
      ['standing_up', '../animations/Standing Up.fbx'],
      ['sit_to_type', '../animations/Sit To Type.fbx'],
      ['typing', '../animations/Typing.fbx'],
      // Filename assumed as 'Thinking.fbx' — rename this line if yours
      // is named differently.
      ['thinking', '../animations/Thinking.fbx'],
      ['pose1', '../animations/Female Dance Pose.fbx'],
      ['pose2', '../animations/Female Dance Pose2.fbx'],
      ['pose3', '../animations/Female Dance Pose3.fbx'],
      ['pose4', '../animations/Female Dance Pose4.fbx'],
      // No Talking.fbx is used by design — speech is shown via lip-sync
      // (setViseme/setMouthLevel) layered on top of whatever pose is
      // already playing, not a dedicated talking-loop clip.
    ];

    // Loaded in parallel — one slow/failed FBX fetch shouldn't stall every
    // clip after it in a serial queue.
    const results = await Promise.allSettled(
      clips.map(([name, path]) => avatar.loadAnimationClip(name, path))
    );
    results.forEach((r, i) => {
      if (r.status === 'rejected') {
        console.warn(`[VRM] Failed to load clip '${clips[i][0]}':`, r.reason);
      }
    });

    if (avatar.hasAction('idle')) {
      avatar.startIdle();
    } else {
      console.warn('[VRM] idle.fbx failed to load — avatar will hold a rest pose until a command plays.');
    }

    window.__animsReady = true;
    console.log('[VRM] All animations ready.');
  } catch (err) {
    console.error('[VRM] Loading sequence error:', err);
    window.__lastError = String(err);
    // Keep the render loop alive even on failure, rather than leaving a
    // frozen/blank canvas.
  }
})();