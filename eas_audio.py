import os
import subprocess
import re
from pydub import AudioSegment
from EASGen import EASGen

def clean_for_dectalk(text):
    """
    Cleans up NWS text so the older DECtalk/ScanSoft engine pronounces it correctly
    and adds appropriate pauses.
    """
    # Replace NWS ellipses with commas for natural pauses
    text = re.sub(r'\.\.\.+', ', ', text)
    
    # Fix time pronunciation (e.g., "4:40 PM" -> "4 40 PM")
    text = re.sub(r'(\d{1,2}):(\d{2})', r'\1 \2', text)
    
    # Ensure space between time and AM/PM if missing
    text = re.sub(r'(\d{2})\s?([AP]M)', r'\1 \2', text, flags=re.IGNORECASE)
    
    # Expand common NWS abbreviations
    replacements = {
        r'\bNWS\b': 'National Weather Service',
        r'\bmph\b': 'miles per hour',
        r'\bMPH\b': 'miles per hour',
        # Timezones
        r'\bEDT\b': 'Eastern Daylight Time',
        r'\bEST\b': 'Eastern Standard Time',
        r'\bCDT\b': 'Central Daylight Time',
        r'\bCST\b': 'Central Standard Time',
        r'\bMDT\b': 'Mountain Daylight Time',
        r'\bMST\b': 'Mountain Standard Time',
        r'\bPDT\b': 'Pacific Daylight Time',
        r'\bPST\b': 'Pacific Standard Time',
        r'\bAKDT\b': 'Alaska Daylight Time',
        r'\bAKST\b': 'Alaska Standard Time',
        r'\bHST\b': 'Hawaii Standard Time',
        # Specific States
        r'\bCO\b': 'Colorado',
        r'\bHI\b': 'Hawaii',
        r'\bTX\b': 'Texas',
        r'\bFL\b': 'Florida',
        r'\bOK\b': 'Oklahoma'
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
        
    return text

def get_available_voices():
    """Returns a list of available SAPI5 voice names using the 32-bit PS bridge."""
    import uuid
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    unique_id = uuid.uuid4().hex[:8]
    ps_script_path = os.path.join(bot_dir, f"temp_list_{unique_id}.ps1")
    
    ps_script = """
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($voice in $synth.GetInstalledVoices()) {
    if ($voice.Enabled) { Write-Output $voice.VoiceInfo.Name }
}
$synth.Dispose()
"""
    with open(ps_script_path, "w", encoding="utf-8") as f:
        f.write(ps_script)
        
    ps_exe = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    try:
        result = subprocess.run([ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script_path], 
                               capture_output=True, text=True, check=True)
        voices = [line.strip() for line in result.stdout.split('\n') if line.strip()]
        return voices
    except Exception as e:
        print(f"Error listing voices: {e}")
        return ["ScanSoft Tom_Full_22kHz"]
    finally:
        if os.path.exists(ps_script_path): os.remove(ps_script_path)

def _generate_balbolka(text, filename, voice_name):
    """Generates audio using Balabolka Console (balcon.exe)."""
    abs_filename = os.path.abspath(filename)
    cleaned_text = clean_for_dectalk(text)
    balcon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balabolka")
    balcon_path = os.path.join(balcon_dir, "balcon.exe")
    if not os.path.exists(balcon_path): return False
    try:
        subprocess.run([balcon_path, "-n", voice_name, "-w", abs_filename, "-t", cleaned_text], check=True, capture_output=True)
        return True
    except: return False

def _generate_sapi5(text, filename, voice_name="ScanSoft Tom_Full_22kHz"):
    import uuid
    abs_filename = os.path.abspath(filename)
    cleaned_text = clean_for_dectalk(text)
    unique_id = uuid.uuid4().hex[:8]
    ps_script_path = os.path.join(os.path.dirname(abs_filename), f"temp_ps_{os.getpid()}_{unique_id}.ps1")
    ps_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile('{abs_filename}')
try {{ $synth.SelectVoice('{voice_name}') }} catch {{ }}
$synth.Speak('{cleaned_text.replace("'", "''")}')
$synth.Dispose()
"""
    with open(ps_script_path, "w", encoding="utf-8") as f: f.write(ps_script)
    ps_exe = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    try:
        subprocess.run([ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script_path], check=True, capture_output=True)
        return True
    except: return False
    finally:
        if os.path.exists(ps_script_path): os.remove(ps_script_path)

def generate_voice_with_fallback(text, filename, voice_name, default_voice="ScanSoft Tom_Full_22kHz"):
    if _generate_sapi5(text, filename, voice_name): return filename
    if _generate_balbolka(text, filename, voice_name): return filename
    if voice_name != default_voice: _generate_sapi5(text, filename, default_voice)
    return filename

def generate_eas_message(text, output_filename="alert.wav", pre_speech=None, voice="ScanSoft Tom_Full_22kHz", intro_path=None, outro_path=None):
    import uuid
    print(f"Generating audio for text: {text} using voice: {voice}")
    
    TARGET_SR = 44100
    def ensure_sr(seg): return seg.set_frame_rate(TARGET_SR) if seg.frame_rate != TARGET_SR else seg

    # 1. SAME Header
    header = ensure_sr(EASGen.genHeader("ZCZC-WXR-EAN-008043+0015-1231234-KDEN/NWS-"))
    
    # 2. Attention signal
    attention = ensure_sr(EASGen.genATTN(8))
    
    # 3. Voice message
    unique_id = uuid.uuid4().hex[:8]
    temp_tts_file = os.path.abspath(f"temp_tts_{unique_id}.wav")
    generate_voice_with_fallback(text, temp_tts_file, voice)
    voice_audio = ensure_sr(AudioSegment.from_wav(temp_tts_file))
    
    # 4. EOM
    eom = ensure_sr(EASGen.genEOM())
    
    # 5. Compile
    silence_short = AudioSegment.silent(duration=500, frame_rate=TARGET_SR)
    silence_long = AudioSegment.silent(duration=1000, frame_rate=TARGET_SR)
    
    final_audio = header + silence_short + attention + silence_long + voice_audio + silence_long + eom
    
    # 6. Pre-speech (if any)
    if pre_speech:
        temp_pre_file = os.path.abspath(f"temp_pre_{unique_id}.wav")
        generate_voice_with_fallback(pre_speech, temp_pre_file, voice)
        pre_voice = ensure_sr(AudioSegment.from_wav(temp_pre_file))
        final_audio = pre_voice + silence_long + final_audio
        if os.path.exists(temp_pre_file): os.remove(temp_pre_file)
        
    # 7. Intro Sound (External file)
    if intro_path and os.path.exists(intro_path):
        intro_sound = ensure_sr(AudioSegment.from_file(intro_path))
        final_audio = intro_sound + silence_long + final_audio
        
    # 8. Outro Sound (External file)
    if outro_path and os.path.exists(outro_path):
        outro_sound = ensure_sr(AudioSegment.from_file(outro_path))
        final_audio = final_audio + silence_long + outro_sound
    
    final_audio.export(output_filename, format="wav")
    if os.path.exists(temp_tts_file): os.remove(temp_tts_file)
    return output_filename

def generate_normal_speech(text, output_filename="speech.wav", voice="ScanSoft Tom_Full_22kHz", intro_path=None, outro_path=None):
    import uuid
    print(f"Generating normal speech for text: {text} using voice: {voice}")
    
    TARGET_SR = 44100
    def ensure_sr(seg): return seg.set_frame_rate(TARGET_SR) if seg.frame_rate != TARGET_SR else seg

    unique_id = uuid.uuid4().hex[:8]
    temp_tts_file = os.path.abspath(f"temp_tts_normal_{unique_id}.wav")
    generate_voice_with_fallback(text, temp_tts_file, voice)
    voice_audio = ensure_sr(AudioSegment.from_wav(temp_tts_file))
    
    silence_500 = AudioSegment.silent(duration=500, frame_rate=TARGET_SR)
    silence_1000 = AudioSegment.silent(duration=1000, frame_rate=TARGET_SR)
    final_audio = silence_500 + voice_audio + silence_500
    
    if intro_path and os.path.exists(intro_path):
        intro_sound = ensure_sr(AudioSegment.from_file(intro_path))
        final_audio = intro_sound + silence_1000 + final_audio
    if outro_path and os.path.exists(outro_path):
        outro_sound = ensure_sr(AudioSegment.from_file(outro_path))
        final_audio = final_audio + silence_1000 + outro_sound
        
    final_audio.export(output_filename, format="wav")
    if os.path.exists(temp_tts_file): os.remove(temp_tts_file)
    return output_filename
