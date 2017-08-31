#!/usr/bin/env python
# -*- encoding: UTF-8 -*-

from sys import argv
from qi import async, bind, nobind, Application, Logger, Signal, Void, String, Bool, Int32, List
from uuid import uuid4
from pyaudio import PyAudio, paInt16, paContinue
from queue import Queue
from collections import deque
from os import environ
from time import sleep
from google.cloud import speech
from google.cloud.speech import enums
from google.cloud.speech import types

GOOGLE_CRED_FILE = "PepperASR-f7d4dd7931ce.json"
BEAM_FORMING_SAMPLE_DIFF = 7
BEAM_FORMING_CHANNELS = 4


class GoogleSpeechRecognition:
    @nobind
    def __init__(self, session=None):
        self._logger = Logger(self.__class__.__name__)

        # initialize internal variables
        self._logger.info("[init ] Initializing service")
        self._subscriber_list = set()
        self._is_robot = (session is not None)
        self._is_running = False
        self._robot_session = session
        self._robot_service_id = None
        self._robot_audio_device = None
        self._computer_audio_interface = None
        self._computer_audio_stream = None
        self._raw_buffer = deque()
        self._input_qty = 0
        self._size_to_filter = 0
        self._filtered_buffer = Queue()
        self._google_rate = 0
        self._google_client = None
        self._google_recognition_config = None
        self._google_response_iterator = None

        # initialize public variables
        self.on_sentence = Signal("(s)")

        # Will raise an exception to stop the service if we are on a virtual robot
        self._raise_if_virtual_robot()

        # Set environment variable for Google credentials...
        environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CRED_FILE

    @nobind
    def __enter__(self):
        self._logger.info("[enter] Connecting service")
        if self._is_robot:
            self._robot_service_id = self._robot_session.registerService(self.__class__.__name__, self)
        return self

    @nobind
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logger.info("[exit ] Disconnecting service")
        self._close_microphones()
        if self._is_robot:
            self._robot_session.unregisterService(self._robot_service_id)

    @nobind
    def __del__(self):
        pass

    @nobind
    def _raise_if_virtual_robot(self):
        if self._is_robot:
            try:
                self._robot_session.service("ALSystem")
                self._logger.info("[init ] Running on a robot, will use the robot's microphones.")
            except RuntimeError:
                raise RuntimeError(
                    'Running on a virtual robot, this service won\'t work! '
                    'Run it on a real robot or on a computer.')
        else:
            self._logger.info("[init ] Running on a computer, will use the computer's microphone.")

    ####################################
    #                                  #
    #        MANAGE SUBSCRIBERS        #
    #                                  #
    ####################################

    @bind(methodName="subscribe", returnType=String, paramsType=[String])
    def subscribe(self, subscriber_name):
        """Subscribe to speech recognition. This call starts the engine.
        The function will return the subscriber name, you need to provide this to unsubscribe.

        :param subscriber_name: str
        :return: str
        """
        # 1) find a unique subscriber name
        final_name = subscriber_name
        while final_name in self._subscriber_list:
            final_name = "{}_{}".format(subscriber_name, str(uuid4())[1:3])
        self._logger.info("[sub  ] New subscriber: {}".format(final_name))
        # 2) add it to the list
        self._subscriber_list.add(final_name)
        # 3) start streaming if not done already
        self._start_streaming()
        # 4) return subscriber name to application
        return final_name

    @bind(methodName="unsubscribe", returnType=Bool, paramsType=[String])
    def unsubscribe(self, subscriber_name):
        """Unsubscribe to speech recognition. Provide the subscriber name as returned by the subscribe function.
        Once all subscribers are removed, the streaming stops.
        Returns True or False depending on if the call was successful.

        :param subscriber_name: str
        :return: boolean
        """
        # 1) remove from subscriber list
        try:
            self._subscriber_list.remove(subscriber_name)
        except IndexError:
            print "Not subscribed"
            return False
        # 2) stop streaming if needed
        if len(self._subscriber_list) == 0:
            self._stop_streaming()
        return True

    @nobind
    def _start_streaming(self):
        if self._is_running:
            return
        self._is_running = True
        self._logger.info("[start] Start streaming")
        async(self._open_microphones)
        async(self._start_google_stream)

    @nobind
    def _stop_streaming(self):
        if not self._is_running:
            return
        self._is_running = False
        self._logger.info("[stop ] Stop streaming")
        async(self._close_microphones)

    ####################################
    #                                  #
    #    GET SOUND FROM MICROPHONES    #
    #                                  #
    ####################################

    @nobind
    def _open_microphones(self):
        if self._is_robot:
            self._logger.info("[open ] Opening robot microphones.")
            # self._google_rate = 48000
            self._google_rate = 16000
            self._robot_audio_device = self._robot_session.service("ALAudioDevice")
            # ask for all microphones signals interleaved sampled at 48kHz
            # self._robot_audio_device.setClientPreferences(self.__class__.__name__, 48000, 0, 0)
            self._robot_audio_device.setClientPreferences(self.__class__.__name__, 16000, 1, 0)
            self._robot_audio_device.subscribe(self.__class__.__name__)
        else:
            self._logger.info("[open ] Opening computer microphones.")
            self._google_rate = 16000
            self._computer_audio_interface = PyAudio()
            self._computer_audio_stream = self._computer_audio_interface.open(
                format=paInt16, channels=1, rate=16000,
                input=True, stream_callback=self._computer_callback,
            )
        self._logger.info("[open ] Done!")

    @nobind
    def _close_microphones(self):
        if self._is_robot:
            pass
        else:
            self._logger.info("[close] Closing computer microphones.")
            self._computer_audio_interface.terminate()

    @nobind
    def _computer_callback(self, in_data, frame_count, time_info, status_flags):
        """ This is the callback used for computer microphone audio buffers """
        # self._logger.info('[proc ] New buffer ({} samples)'.format(frame_count))
        self._filtered_buffer.put(in_data)
        return None, paContinue

    # todo: rename this process_remote
    @bind(methodName="processRemote", returnType=Void, paramsType=[Int32, Int32, Int32, List(Int32)])
    def processRemote(self, nr_of_channels, nr_of_samples_per_channel, timestamp, input_buffer):
        """ This is the callback that receives the robots audio buffers """
        # self._logger.info('[proc ] New buffer received ({} channels x {} samples)'.format(nr_of_channels,
        #                                                                                   nr_of_samples_per_channel))
        self._filtered_buffer.put(input_buffer)
        return
        self._raw_buffer.extend(input_buffer)
        self._input_qty += nr_of_samples_per_channel
        if self._input_qty > BEAM_FORMING_SAMPLE_DIFF:
             async(self._filter_buffer)
             self._size_to_filter = self._input_qty
             self._input_qty = 0
        # self._logger.info("[proc ] finished")

    @nobind
    def _filter_buffer(self):
        # self._logger.verbose('[filtr] Filtering raw values...')
        # calculate how many samples we can get (need to keep a rolling buffer for beam forming)
        n_samples = self._size_to_filter - BEAM_FORMING_SAMPLE_DIFF
        # copying those samples into a list
        raw_buffer = []
        # deleting them from the queue, except the ones for next beam-forming, which are put back on the left
        for i in range(n_samples * BEAM_FORMING_CHANNELS):
            raw_buffer.append(self._raw_buffer.popleft())
        cross_values = []
        for i in range(BEAM_FORMING_SAMPLE_DIFF * BEAM_FORMING_CHANNELS):
            v = self._raw_buffer.popleft()
            raw_buffer.append(v)
            cross_values.append(v)
        cross_values.reverse()
        for i in cross_values:
            self._raw_buffer.appendleft(i)
        # apply the filter "delay and sum beam forming"
        for i in range(n_samples):
            self._filtered_buffer.put(
                (raw_buffer[i * BEAM_FORMING_CHANNELS]
                 + raw_buffer[i * BEAM_FORMING_CHANNELS + 1]
                 + raw_buffer[(i + BEAM_FORMING_SAMPLE_DIFF) * BEAM_FORMING_CHANNELS + 2]
                 + raw_buffer[(i + BEAM_FORMING_SAMPLE_DIFF) * BEAM_FORMING_CHANNELS + 3]
                 ) / BEAM_FORMING_CHANNELS)
        # self._logger.verbose('[filtr] Finished')

    ####################################
    #                                  #
    #     STREAM BUFFER TO GOOGLE      #
    #                                  #
    ####################################

    @nobind
    def _generate_next_buffer(self):
        while self._is_running:
            # self._logger.info("[next ] Generate next data!")
            # yield types.StreamingRecognizeRequest(audio_content=self._filtered_buffer.get())
            audio_data = [str(self._filtered_buffer.get())]
            while not self._filtered_buffer.empty():
                audio_data.append(str(self._filtered_buffer.get()))
            yield types.StreamingRecognizeRequest(audio_content=b''.join(audio_data))

    @nobind
    def _start_google_stream(self):
        self._logger.info("[gstar] Start streaming to Google")
        # Configure Google speech recognition
        self._google_client = speech.SpeechClient()
        self._logger.info("[gstar] Got Google client")
        contexts = [types.SpeechContext(
            phrases=[]
        )]
        config = types.RecognitionConfig(
            encoding=enums.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self._google_rate,
            language_code="en_US",
            max_alternatives=1,
            profanity_filter=False,
            speech_contexts=contexts,
            enable_word_time_offsets=False
        )
        self._google_recognition_config = types.StreamingRecognitionConfig(
            config=config,
            single_utterance=False,
            interim_results=False
        )
        self._logger.info("[gstar] Google configuration ready")
        source_audio = (types.StreamingRecognizeRequest(audio_content=content)
                        for content in self._generate_next_buffer())
        self._logger.info("[gstar] source list ready")
        self._google_response_iterator = self._google_client.streaming_recognize(self._google_recognition_config,
                                                                                 self._generate_next_buffer())
                                                                          # source_audio)
        self._logger.info("[gstar] Streaming started!")
        async(self._process_next_response)

    @nobind
    def _process_next_response(self):
        self._logger.info("[gresp] Waiting for next response...")
        if not self._is_running:
            return
        streaming_recognize_response = self._google_response_iterator.next()
        self._logger.info("[gresp] Got a response!")
        if not self._is_running or not streaming_recognize_response:
            return
        # if streaming_recognize_response.error:
        #     print streaming_recognize_response
        #     print streaming_recognize_response.error
        #     self._logger.info("[gresp] error: {}".format(streaming_recognize_response.error))
        # elif streaming_recognize_response.speech_event_type:
        #     self._logger.info("[gresp] event: {}".format(streaming_recognize_response.speech_event_type))
        if streaming_recognize_response.results:
            # self._logger.info("[gresp] result: {}".format(streaming_recognize_response.results))
            for result in streaming_recognize_response.results:
                async(self._process_valid_result, result)
        async(self._process_next_response)

    @nobind
    def _process_valid_result(self, result):
        if result.is_final:
            self._logger.info("[valid] *** New final result ***")
            alternative = result.alternatives[0]  # there is only 1 because max_alternatives=1
            self._logger.info(alternative.transcript)
            self._logger.info(alternative.confidence)
            self.on_sentence([alternative.transcript, alternative.confidence])

if __name__ == '__main__':
    qi_app = Application(argv)
    qi_session = None
    try:
        qi_app.start()
        qi_session = qi_app.session
    except RuntimeError:
        pass
    with GoogleSpeechRecognition(qi_session) as g:
        name = g.subscribe("hello")
        while True:
            sleep(1)

