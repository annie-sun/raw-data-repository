import datetime
import httplib
import json

from code_constants import PPI_EXTRA_SYSTEM
from clock import FakeClock
from dao.code_dao import CodeDao
from dao.questionnaire_dao import QuestionnaireDao
from dao.questionnaire_response_dao import QuestionnaireResponseAnswerDao
from model.utils import from_client_participant_id
from model.questionnaire_response import QuestionnaireResponseAnswer
from test.unit_test.unit_test_util import (
    FlaskTestBase,
    make_questionnaire_response_json as gen_response
)
from test.test_data import data_path


TIME_1 = datetime.datetime(2016, 1, 1)
TIME_2 = datetime.datetime(2016, 1, 2)


def _questionnaire_response_url(participant_id):
  return 'Participant/%s/QuestionnaireResponse' % participant_id


class QuestionnaireResponseApiTest(FlaskTestBase):

  def test_insert_raises_400_for_excessively_long_valueString(self):
    participant_id = self.create_participant()
    questionnaire_id = self.create_questionnaire('questionnaire1.json')
    url = _questionnaire_response_url(participant_id)

    # Remember we need to send the consent first
    self.send_consent(participant_id)

    # Check that a string of exactly the max length will post
    # This one should be exactly long enough to pass
    string = 'a' * QuestionnaireResponseAnswer.VALUE_STRING_MAXLEN
    string_answers = [["nameOfChild", string]]
    resource = gen_response(participant_id, questionnaire_id, string_answers=string_answers)
    response = self.send_post(url, resource)
    self.assertEquals(response['group']['question'][0]['answer'][0]['valueString'], string)

    # Check that a string longer than the max will not
    # This one should evaluate to a string that is one char too long; i.e. exactly 64KiB
    string = 'a' * (QuestionnaireResponseAnswer.VALUE_STRING_MAXLEN + 1)
    string_answers = [["nameOfChild", string]]
    resource = gen_response(participant_id, questionnaire_id, string_answers=string_answers)
    self.send_post(url, resource, expected_status=httplib.BAD_REQUEST)

  def test_insert(self):
    participant_id = self.create_participant()
    questionnaire_id = self.create_questionnaire('questionnaire1.json')
    with open(data_path('questionnaire_response3.json')) as fd:
      resource = json.load(fd)

    # Sending response with the dummy participant id in the file is an error
    self.send_post(_questionnaire_response_url('{participant_id}'), resource,
                   expected_status=httplib.NOT_FOUND)

    # Fixing participant id but not the questionnaire id is also an error
    resource['subject']['reference'] = \
        resource['subject']['reference'].format(participant_id=participant_id)
    self.send_post(_questionnaire_response_url(participant_id), resource,
                   expected_status=httplib.BAD_REQUEST)

    # Fix the reference
    resource['questionnaire']['reference'] = \
        resource['questionnaire']['reference'].format(questionnaire_id=questionnaire_id)

    # Sending the response before the consent is an error.
    self.send_post(_questionnaire_response_url(participant_id), resource,
                   expected_status=httplib.BAD_REQUEST)

    # After consent, the post succeeds
    self.send_consent(participant_id)
    response = self.send_post(_questionnaire_response_url(participant_id), resource)
    resource['id'] = response['id']
    # The resource gets rewritten to include the version
    resource['questionnaire']['reference'] = 'Questionnaire/%s/_history/1' % questionnaire_id
    self.assertJsonResponseMatches(resource, response)

    # Do a get to fetch the questionnaire
    get_response = self.send_get(_questionnaire_response_url(participant_id) + "/" + response['id'])
    self.assertJsonResponseMatches(resource, get_response)

    code_dao = CodeDao()

    # Ensure we didn't create codes in the extra system
    self.assertIsNone(code_dao.get_code(PPI_EXTRA_SYSTEM, 'IgnoreThis'))

    name_of_child = code_dao.get_code("sys", "nameOfChild")
    birth_weight = code_dao.get_code("sys", "birthWeight")
    birth_length = code_dao.get_code("sys", "birthLength")
    vitamin_k_dose_1 = code_dao.get_code("sys", "vitaminKDose1")
    vitamin_k_dose_2 = code_dao.get_code("sys", "vitaminKDose2")
    hep_b_given = code_dao.get_code("sys", "hepBgiven")
    abnormalities_at_birth = code_dao.get_code("sys", "abnormalitiesAtBirth")
    answer_dao = QuestionnaireResponseAnswerDao()
    with answer_dao.session() as session:
      code_ids = [code.codeId for code in
                  [name_of_child, birth_weight, birth_length, vitamin_k_dose_1, vitamin_k_dose_2,
                  hep_b_given, abnormalities_at_birth]]
      current_answers = answer_dao.get_current_answers_for_concepts(session,\
          from_client_participant_id(participant_id), code_ids)
    self.assertEquals(7, len(current_answers))
    questionnaire = QuestionnaireDao().get_with_children(questionnaire_id)
    question_id_to_answer = {answer.questionId : answer for answer in current_answers}
    code_id_to_answer = {question.codeId:
                         question_id_to_answer.get(question.questionnaireQuestionId)
                         for question in questionnaire.questions}
    self.assertEquals("Cathy Jones", code_id_to_answer[name_of_child.codeId].valueString)
    self.assertEquals(3.25, code_id_to_answer[birth_weight.codeId].valueDecimal)
    self.assertEquals(44.3, code_id_to_answer[birth_length.codeId].valueDecimal)
    self.assertEquals(44, code_id_to_answer[birth_length.codeId].valueInteger)
    self.assertEquals(True, code_id_to_answer[hep_b_given.codeId].valueBoolean)
    self.assertEquals(0, code_id_to_answer[abnormalities_at_birth.codeId].valueInteger)
    self.assertEquals(datetime.date(1972, 11, 30),
                      code_id_to_answer[vitamin_k_dose_1.codeId].valueDate)
    self.assertEquals(datetime.datetime(1972, 11, 30, 12, 34, 42),
                      code_id_to_answer[vitamin_k_dose_2.codeId].valueDateTime)

  def test_demographic_questionnaire_responses(self):
    with FakeClock(TIME_1):
      participant_id = self.create_participant()
      self.send_consent(participant_id)
    questionnaire_id = self.create_questionnaire('questionnaire_demographics.json')
    with open(data_path('questionnaire_response_demographics.json')) as f:
      resource = json.load(f)
    resource['subject']['reference'] = \
      resource['subject']['reference'].format(participant_id=participant_id)
    resource['questionnaire']['reference'] = \
      resource['questionnaire']['reference'].format(questionnaire_id=questionnaire_id)
    with FakeClock(TIME_2):
      self.send_post(_questionnaire_response_url(participant_id), resource)

    participant = self.send_get('Participant/%s' % participant_id)
    summary = self.send_get('Participant/%s/Summary' % participant_id)
    expected = {'ageRange': 'UNSET',
                'genderIdentity': 'UNMAPPED',
                'firstName': self.first_name,
                'lastName': self.last_name,
                'email': self.email,
                'race': 'UNSET',
                'hpoId': 'UNSET',
                'awardee': 'UNSET',
                'site': 'UNSET',
                'organization': 'UNSET',
                'education': 'UNSET',
                'income': 'UNSET',
                'language': 'UNSET',
                'sex': 'UNSET',
                'sexualOrientation': 'UNSET',
                'state': 'UNSET',
                'recontactMethod': 'UNSET',
                'enrollmentStatus': 'INTERESTED',
                'samplesToIsolateDNA': 'UNSET',
                'numBaselineSamplesArrived': 0,
                'numCompletedPPIModules': 1,
                'numCompletedBaselinePPIModules': 1,
                'biobankId': participant['biobankId'],
                'participantId': participant_id,
                'physicalMeasurementsStatus': 'UNSET',
                'consentForElectronicHealthRecords': 'UNSET',
                'consentForStudyEnrollment': 'SUBMITTED',
                'consentForStudyEnrollmentTime': TIME_1.isoformat(),
                'consentForCABoR': 'UNSET',
                'questionnaireOnFamilyHealth': 'UNSET',
                'questionnaireOnHealthcareAccess': 'UNSET',
                'questionnaireOnMedicalHistory' : 'UNSET',
                'questionnaireOnMedications': 'UNSET',
                'questionnaireOnOverallHealth': 'UNSET',
                'questionnaireOnLifestyle': 'UNSET',
                'questionnaireOnTheBasics': 'SUBMITTED',
                'questionnaireOnTheBasicsTime': TIME_2.isoformat(),
                'biospecimenCollectedSite': 'UNSET',
                'biospecimenFinalizedSite': 'UNSET',
                'biospecimenProcessedSite': 'UNSET',
                'biospecimenSourceSite': 'UNSET',
                'physicalMeasurementsCreatedSite': 'UNSET',
                'physicalMeasurementsFinalizedSite': 'UNSET',
                'biospecimenStatus': 'UNSET',
                'sampleOrderStatus1ED04': 'UNSET',
                'sampleOrderStatus1ED10': 'UNSET',
                'sampleOrderStatus1HEP4': 'UNSET',
                'sampleOrderStatus1PST8': 'UNSET',
                'sampleOrderStatus1PS08': 'UNSET',
                'sampleOrderStatus2PST8': 'UNSET',
                'sampleOrderStatus1SAL': 'UNSET',
                'sampleOrderStatus1SST8': 'UNSET',
                'sampleOrderStatus2SST8': 'UNSET',
                'sampleOrderStatus1SS08': 'UNSET',
                'sampleOrderStatus1UR10': 'UNSET',
                'sampleOrderStatus2ED10': 'UNSET',
                'sampleStatus1ED04': 'UNSET',
                'sampleStatus1ED10': 'UNSET',
                'sampleStatus1HEP4': 'UNSET',
                'sampleStatus1PST8': 'UNSET',
                'sampleStatus2PST8': 'UNSET',
                'sampleStatus1PS08': 'UNSET',
                'sampleStatus1SAL': 'UNSET',
                'sampleStatus1SST8': 'UNSET',
                'sampleStatus2SST8': 'UNSET',
                'sampleStatus1SS08': 'UNSET',
                'sampleStatus1UR10': 'UNSET',
                'sampleStatus2ED10': 'UNSET',
                'samplesToIsolateDNA': 'UNSET',
                'signUpTime': TIME_1.isoformat(),
                'withdrawalStatus': 'NOT_WITHDRAWN',
                'suspensionStatus': 'NOT_SUSPENDED',
              }
    self.assertJsonResponseMatches(expected, summary)
