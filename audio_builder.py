import soundfile
import pyrubberband
import configparser
import pathlib
import os
import io

import TTS
from utils import parseBool

from pydub import AudioSegment
from pydub.silence import detect_leading_silence
import langcodes

# Set working folder
workingFolder = "workingFolder"

# Read config files
config = configparser.ConfigParser()
config.read('config.ini')
batchConfig = configparser.ConfigParser()
batchConfig.read('batch.ini')
cloudConfig = configparser.ConfigParser()
cloudConfig.read('cloud_service_settings.ini')

# Get variables from configs
nativeSampleRate = int(config['SETTINGS']['synth_sample_rate'])

skipSynthesize = parseBool(config['SETTINGS']['skip_synthesize'])
forceTwoPassStretch = parseBool(config['SETTINGS']['force_stretch_with_twopass'])
outputFormat = config['SETTINGS']['output_format'].lower()
batchSynthesize = parseBool(cloudConfig['CLOUD']['batch_tts_synthesize'])
tts_service = cloudConfig['CLOUD']['tts_service']
debugMode = parseBool(config['SETTINGS']['debug_mode'])

# MOVE THIS INTO A VARIABLE AT SOME POINT
# Get original video file path, also allow you to debug using a subtitle file without having the original video file
videoFilePath = batchConfig['SETTINGS']['original_video_file_path']
originalVideoFile = os.path.abspath(batchConfig['SETTINGS']['original_video_file_path'].strip("\""))
if debugMode and (videoFilePath == '' or videoFilePath.lower() == 'none'):
    originalVideoFile = 'Debug.test'
else:
    originalVideoFile = os.path.abspath(videoFilePath.strip("\""))
outputDirectory = "Outputs"
outputFolder = os.path.join(outputDirectory , os.path.splitext(os.path.basename(originalVideoFile))[0])

def trim_clip(inputSound):
    trim_leading_silence: AudioSegment = lambda x: x[detect_leading_silence(x) :]
    trim_trailing_silence: AudioSegment = lambda x: trim_leading_silence(x.reverse()).reverse()
    strip_silence: AudioSegment = lambda x: trim_trailing_silence(trim_leading_silence(x))
    strippedSound = strip_silence(inputSound)
    return strippedSound

# Function to insert audio into canvas at specific point
def insert_audio(canvas, audioToOverlay, startTimeMs):
    # Create a copy of the canvas
    canvasCopy = canvas
    # Overlay the audio onto the copy
    canvasCopy = canvasCopy.overlay(audioToOverlay, position=int(startTimeMs))
    # Return the copy
    return canvasCopy

# Function to create a canvas of a specific duration in miliseconds
def create_canvas(canvasDuration, frame_rate=nativeSampleRate):
    canvas = AudioSegment.silent(duration=canvasDuration, frame_rate=frame_rate)
    return canvas

def get_speed_factor(subsDict, trimmedAudio, desiredDuration, num):
    virtualTempFile = AudioSegment.from_file(trimmedAudio, format="wav")
    rawDuration = virtualTempFile.duration_seconds
    trimmedAudio.seek(0) # This MUST be done to reset the file pointer to the start of the file, otherwise will get errors next time try to access the virtual files
    # Calculate the speed factor, put into dictionary
    desiredDuration = float(desiredDuration)
    speedFactor = (rawDuration*1000) / desiredDuration
    subsDict[num]['speed_factor'] = speedFactor
    return subsDict

def stretch_audio(audioFileToStretch, speedFactor, num):
    virtualTempAudioFile = io.BytesIO()
    # Write the raw string to virtualtempaudiofile
    y, sampleRate = soundfile.read(audioFileToStretch)

    streched_audio = pyrubberband.time_stretch(y, sampleRate, speedFactor, rbargs={'--fine': '--fine'}) # Need to add rbarges in weird way because it demands a dictionary of two values
    #soundfile.write(f'{workingFolder}\\temp_stretched.wav', streched_audio, sampleRate)
    soundfile.write(virtualTempAudioFile, streched_audio, sampleRate, format='wav')
    if debugMode:
        soundfile.write(os.path.join(workingFolder, f'{num}_s.wav'), streched_audio, sampleRate) # For debugging, saves the stretched audio files
    #return AudioSegment.from_file(f'{workingFolder}\\temp_stretched.wav', format="wav")
    return AudioSegment.from_file(virtualTempAudioFile, format="wav")


def build_audio(subsDict, langDict, totalAudioLength, twoPassVoiceSynth=False):
    virtualTrimmedFileDict = {}
    # First trim silence off the audio files
    for key, value in subsDict.items():
        filePathTrimmed = os.path.join(workingFolder,  str(key)) + "_t.wav"
        subsDict[key]['TTS_FilePath_Trimmed'] = filePathTrimmed

        # Trim the clip and re-write file
        rawClip = AudioSegment.from_file(value['TTS_FilePath'], format="mp3", frame_rate=nativeSampleRate)
        trimmedClip = trim_clip(rawClip)
        if debugMode:
            trimmedClip.export(filePathTrimmed, format="wav")

        # Create virtual file in dictionary with audio to be read later
        tempTrimmedFile = io.BytesIO()
        trimmedClip.export(tempTrimmedFile, format="wav")
        virtualTrimmedFileDict[key] = tempTrimmedFile
        keyIndex = list(subsDict.keys()).index(key)
        print(f" Trimmed Audio: {keyIndex+1} of {len(subsDict)}", end="\r")
    print("\n")

    # Calculate speed factors for each clip, aka how much to stretch the audio
    for key, value in subsDict.items():
        #subsDict = get_speed_factor(subsDict, value['TTS_FilePath_Trimmed'], value['duration_ms'], num=key)
        subsDict = get_speed_factor(subsDict, virtualTrimmedFileDict[key], value['duration_ms'], num=key)
        keyIndex = list(subsDict.keys()).index(key)
        print(f" Calculated Speed Factor: {keyIndex+1} of {len(subsDict)}", end="\r")
    print("\n")

    # If two pass voice synth is enabled, have API re-synthesize the clips at the new speed
    if twoPassVoiceSynth == True:
        if batchSynthesize == True and tts_service == 'azure':
            subsDict = TTS.synthesize_dictionary_batch(subsDict, langDict, skipSynthesize=skipSynthesize, secondPass=True)
        else:
            subsDict = TTS.synthesize_dictionary(subsDict, langDict, skipSynthesize=skipSynthesize, secondPass=True)
            
        for key, value in subsDict.items():
            # Trim the clip and re-write file
            rawClip = AudioSegment.from_file(value['TTS_FilePath'], format="mp3", frame_rate=nativeSampleRate)
            trimmedClip = trim_clip(rawClip)
            if debugMode:
                # Remove '.wav' from the end of the file path
                secondPassTrimmedFile = value['TTS_FilePath_Trimmed'][:-4] + "_p2_t.wav"
                trimmedClip.export(secondPassTrimmedFile, format="wav")
            trimmedClip.export(virtualTrimmedFileDict[key], format="wav")
            keyIndex = list(subsDict.keys()).index(key)
            print(f" Trimmed Audio (2nd Pass): {keyIndex+1} of {len(subsDict)}", end="\r")
        print("\n")

        if forceTwoPassStretch == True:
            for key, value in subsDict.items():
                subsDict = get_speed_factor(subsDict, virtualTrimmedFileDict[key], value['duration_ms'], num=key)
                keyIndex = list(subsDict.keys()).index(key)
                print(f" Calculated Speed Factor (2nd Pass): {keyIndex+1} of {len(subsDict)}", end="\r")
            print("\n")

    # Create canvas to overlay audio onto
    canvas = create_canvas(totalAudioLength)

    # Stretch audio and insert into canvas
    for key, value in subsDict.items():
        if not twoPassVoiceSynth or forceTwoPassStretch == True:
            #stretchedClip = stretch_audio(value['TTS_FilePath_Trimmed'], speedFactor=subsDict[key]['speed_factor'], num=key)
            stretchedClip = stretch_audio(virtualTrimmedFileDict[key], speedFactor=subsDict[key]['speed_factor'], num=key)
        else:
            #stretchedClip = AudioSegment.from_file(value['TTS_FilePath_Trimmed'], format="wav")
            stretchedClip = AudioSegment.from_file(virtualTrimmedFileDict[key], format="wav")
            virtualTrimmedFileDict[key].seek(0) # Not 100% sure if this is necessary but it was in the other place it is used

        canvas = insert_audio(canvas, stretchedClip, value['start_ms'])
        keyIndex = list(subsDict.keys()).index(key)
        print(f" Final Audio Processed: {keyIndex+1} of {len(subsDict)}", end="\r")
    print("\n")

    # Use video file name to use in the name of the output file. Add language name and language code
    lang = langcodes.get(langDict['languageCode'])
    langName = langcodes.get(langDict['languageCode']).get(lang.to_alpha3()).display_name()
    if debugMode and not os.path.isfile(originalVideoFile):
        outputFileName = "debug" + f" - {langName} - {langDict['languageCode']}."
    else:
        outputFileName = pathlib.Path(originalVideoFile).stem + f" - {langName} - {langDict['languageCode']}."
    # Set output path
    outputFileName = os.path.join(outputFolder, outputFileName)

    # Determine string to use for output format and file extension based on config setting
    if outputFormat == "mp3":
        outputFileName += "mp3"
        formatString = "mp3"
    elif outputFormat == "wav":
        outputFileName += "wav"
        formatString = "wav"
    elif outputFormat == "aac":
        #outputFileName += "m4a"
        #formatString = "mp4" # Pydub doesn't accept "aac" as a format, so we have to use "mp4" instead. Alternatively, could use "adts" with file extension "aac"
        outputFileName += "aac"
        formatString = "adts" # Pydub doesn't accept "aac" as a format, so we have to use "mp4" instead. Alternatively, could use "adts" with file extension "aac"

    canvas = canvas.set_channels(2) # Change from mono to stereo
    try:
        print("\nExporting audio file...")
        canvas.export(outputFileName, format=formatString, bitrate="192k")
    except:
        outputFileName = outputFileName + ".bak"
        canvas.export(outputFileName, format=formatString, bitrate="192k")
        print("\nThere was an issue exporting the audio, it might be a permission error. The file was saved as a backup with the extension .bak")
        print("Try removing the .bak extension then listen to the file to see if it worked.\n")
        input("Press Enter to exit...")

    return subsDict
