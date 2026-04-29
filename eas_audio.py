import os
import subprocess
import re
import numpy as np
from pydub import AudioSegment
from pydub.generators import Sine
from EASGen import EASGen

def clean_for_dectalk(text):
    """
    Cleans up NWS text so the older DECtalk/ScanSoft engine pronounces it correctly
    and adds appropriate pauses.
    """
    # Replace NWS ellipses with commas for natural pauses
    text = re.sub(r'\.\.\.+', ', ', text)
    
    # Fix time pronunciation (e.g., "4:40 PM" -> "4 40 PM")
    # This prevents the engine from saying "four-hundred-forty" or merging them.
    text = re.sub(r'(\d{1,2}):(\d{2})', r'\1 \2', text)
    
    # Ensure space between time and AM/PM if missing (e.g., "4 40PM" -> "4 40 PM")
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
        # Specific States requested / problematic
        r'\bCO\b': 'Colorado',
        r'\bHI\b': 'Hawaii',
        r'\bTX\b': 'Texas',
        r'\bFL\b': 'Florida',
        r'\bOK\b': 'Oklahoma'
    }
    
    # Do a case-sensitive replacement for the exact matches above
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
        
    return text

def generate_radio_atmosphere(duration_ms, volume_db=-30):
    """Generates a layer of white noise and low hum to simulate weather radio."""
    # Generate white noise using numpy
    sample_rate = 44100
    n_samples = int(sample_rate * (duration_ms / 1000.0))
    noise_data = np.random.uniform(-1, 1, n_samples).astype(np.float32)
    
    # Convert to 16-bit PCM for pydub
    noise_data = (noise_data * 32767).astype(np.int16)
    noise_segment = AudioSegment(
        noise_data.tobytes(), 
        frame_rate=sample_rate,
        sample_width=2, 
        channels=1
    )
    
    # Generate a low 60Hz hum (electronic interference)
    hum = Sine(60).to_audio_segment(duration=duration_ms).apply_gain(-40)
    
    # Combine and set base volume (very quiet background)
    return (noise_segment.overlay(hum)).apply_gain(volume_db)

def generate_mic_click():
    """Generates a short radio 'key up' click."""
    duration = 40
    sample_rate = 44100
    n_samples = int(sample_rate * (duration / 1000.0))
    click_data = np.random.uniform(-1, 1, n_samples).astype(np.float32)
    click_data = (click_data * 32767).astype(np.int16)
    
    click = AudioSegment(
        click_data.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,
        channels=1
    )
    return click.apply_gain(-10) # Sharp but not deafening

def apply_radio_filter(audio_segment):
    """Overlays radio static and adds mic clicks to an audio segment."""
    static = generate_radio_atmosphere(len(audio_segment))
    click_in = generate_mic_click()
    click_out = generate_mic_click()
    
    # Blend voice with static
    filtered = audio_segment.overlay(static)
    
    # Add clicks at the very start and end
    return click_in + filtered + click_out

def get_available_voices():
    """Returns a list of available SAPI5 voice names using the 32-bit PS bridge."""
    import uuid
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    ps_script_path = os.path.join(bot_dir, f"temp_list_{uuid.uuid4().hex[:8]}.ps1")
    
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
    
    # Get path to balcon.exe
    balcon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balabolka")
    balcon_path = os.path.join(balcon_dir, "balcon.exe")
    
    if not os.path.exists(balcon_path):
        raise FileNotFoundError("Balabolka Console (balcon.exe) not found.")
        
    # balcon command: -n (name), -w (wav file), -t (text)
    try:
        subprocess.run([balcon_path, "-n", voice_name, "-w", abs_filename, "-t", cleaned_text], check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"Balabolka Error: {e}")
        return False

def _generate_sapi5(text, filename, voice_name="ScanSoft Tom_Full_22kHz"):
    import uuid
    abs_filename = os.path.abspath(filename)
    cleaned_text = clean_for_dectalk(text)
    
    unique_id = str(uuid.uuid4())[:8]
    ps_script_path = os.path.join(os.path.dirname(abs_filename), f"temp_ps_{os.getpid()}_{unique_id}.ps1")
    
    # PowerShell script to select voice and speak to file
    # We use ErrorAction Stop to ensure failure triggers the fallback
    ps_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile('{abs_filename}')
$synth.SelectVoice('{voice_name}')
$synth.Speak('{cleaned_text.replace("'", "''")}')
$synth.Dispose()
"""
    with open(ps_script_path, "w", encoding="utf-8") as f:
        f.write(ps_script)
        
    ps_exe = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    try:
        subprocess.run([ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script_path], check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"SAPI5 Error for {voice_name}: {e}")
        return False
    finally:
        if os.path.exists(ps_script_path): os.remove(ps_script_path)

def generate_voice_with_fallback(text, filename, voice_name, default_voice="ScanSoft Tom_Full_22kHz"):
    """
    Tries SAPI5 first, then Balabolka.
    If both fail, it tries the default voice.
    """
    # 1. Try SAPI5 with chosen voice
    if _generate_sapi5(text, filename, voice_name):
        return filename
        
    # 2. Try Balabolka with chosen voice
    print(f"Falling back to Balabolka for {voice_name}...")
    if _generate_balbolka(text, filename, voice_name):
        return filename
        
    # 3. Final Fallback: SAPI5 with Default Voice
    if voice_name != default_voice:
        print(f"Final Fallback: Using default voice {default_voice}...")
        _generate_sapi5(text, filename, default_voice)
        
    return filename

def generate_eas_message(text, output_filename="alert.mp3", pre_speech=None, voice="ScanSoft Tom_Full_22kHz", intro_path=None, outro_path=None):
    print(f"Generating audio for text: {text} using voice: {voice}")
    
    # 1. SAME Header
    header_text = "ZCZC-WXR-EAN-008043+0015-1231234-KDEN/NWS-"
    header = EASGen.genHeader(header_text)
    
    # 2. Attention signal
    attention = EASGen.genATTN(8)
    
    # 3. Voice message (with fallback logic and radio filter)
    temp_tts_file = "temp_tts.wav"
    generate_voice_with_fallback(text, temp_tts_file, voice)
    voice_raw = AudioSegment.from_wav(temp_tts_file)
    voice_filtered = apply_radio_filter(voice_raw)
    
    # 4. EOM
    eom = EASGen.genEOM()
    
    # 5. Compile
    silence_short = AudioSegment.silent(duration=500)
    silence_long = AudioSegment.silent(duration=1000)
    
    final_audio = header + silence_short + attention + silence_long + voice_filtered + silence_long + eom
    
    # 6. Pre-speech (if any)
    if pre_speech:
        temp_pre_file = "temp_pre.wav"
        generate_voice_with_fallback(pre_speech, temp_pre_file, voice)
        pre_voice = apply_radio_filter(AudioSegment.from_wav(temp_pre_file))
        final_audio = pre_voice + silence_long + final_audio
        if os.path.exists(temp_pre_file): os.remove(temp_pre_file)
        
    # 7. Intro Sound (External file)
    if intro_path and os.path.exists(intro_path):
        intro_sound = AudioSegment.from_file(intro_path)
        final_audio = intro_sound + silence_long + final_audio
        
    # 8. Outro Sound (External file)
    if outro_path and os.path.exists(outro_path):
        outro_sound = AudioSegment.from_file(outro_path)
        final_audio = final_audio + silence_long + outro_sound
    
    final_audio.export(output_filename, format="mp3")
    if os.path.exists(temp_tts_file): os.remove(temp_tts_file)
    return output_filename

def generate_normal_speech(text, output_filename="speech.mp3", voice="ScanSoft Tom_Full_22kHz", intro_path=None, outro_path=None):
    print(f"Generating normal speech for text: {text} using voice: {voice}")
    temp_tts_file = "temp_tts_normal.wav"
    generate_voice_with_fallback(text, temp_tts_file, voice)
    voice_raw = AudioSegment.from_wav(temp_tts_file)
    
    # Apply the radio radio atmosphere
    voice_filtered = apply_radio_filter(voice_raw)
    
    silence = AudioSegment.silent(duration=500)
    final_audio = silence + voice_filtered + silence
    
    # Intro Sound (External file)
    if intro_path and os.path.exists(intro_path):
        intro_sound = AudioSegment.from_file(intro_path)
        final_audio = intro_sound + AudioSegment.silent(duration=1000) + final_audio
        
    # Outro Sound (External file)
    if outro_path and os.path.exists(outro_path):
        outro_sound = AudioSegment.from_file(outro_path)
        final_audio = final_audio + AudioSegment.silent(duration=1000) + outro_sound
        
    final_audio.export(output_filename, format="mp3")
    if os.path.exists(temp_tts_file): os.remove(temp_tts_file)
    return output_filename
